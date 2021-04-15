#
# Copyright (c) 2012-2021 Snowflake Computing Inc. All right reserved.
#
import abc
import io
import json
import time
from base64 import b64decode
from enum import Enum, unique
from gzip import GzipFile
from logging import getLogger
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .arrow_context import ArrowConverterContext
from .arrow_iterator import ROW_UNIT, TABLE_UNIT
from .errorcode import ER_FAILED_TO_CONVERT_ROW_TO_PYTHON_TYPE
from .errors import Error, InterfaceError
from .time_util import DecorrelateJitterBackoff, TimerContextManager
from .vendored import requests

logger = getLogger(__name__)

MAX_DOWNLOAD_RETRY = 10
DOWNLOAD_TIMEOUT = 7  # seconds

if TYPE_CHECKING:  # pragma: no cover
    from .converter import SnowflakeConverterType
    from .cursor import SnowflakeCursor

# qrmk related constants
SSE_C_ALGORITHM = "x-amz-server-side-encryption-customer-algorithm"
SSE_C_KEY = "x-amz-server-side-encryption-customer-key"
SSE_C_AES = "AES256"


@unique
class DownloadMetrics(Enum):
    """Defines the keywords by which to store metrics for chunks."""

    download = "download"  # Download time in milliseconds
    parse = "parse"  # Parsing time to final data types
    load = "load"  # Parsing time from initial type to intermediate types


class RemoteChunkInfo(NamedTuple):
    """Small class that holds information about chunks that are given by back-end."""

    url: str
    uncompressedSize: int
    compressedSize: int


def create_chunks_from_response(
    cursor: "SnowflakeCursor",
    _format: str,
    data: Dict[str, Any],
) -> List["ResultChunk"]:
    column_converters: List[Tuple[str, "SnowflakeConverterType"]] = []
    arrow_context: Optional["ArrowConverterContext"] = None
    rowtypes = data["rowtype"]
    column_names: List[str] = [c["name"] for c in rowtypes]
    if _format == "json":
        column_converters: List[Tuple[str, "SnowflakeConverterType"]] = [
            (
                c["type"],
                cursor._connection.converter.to_python_method(c["type"].upper(), c),
            )
            for c in rowtypes
        ]
        first_chunk = JSONResultChunk.from_data(
            data.get("rowset"),
            column_names,
            column_converters,
            cursor._use_dict_result,
        )
    else:
        rowset_b64 = data.get("rowsetBase64")
        arrow_context = ArrowConverterContext(cursor._connection._session_parameters)

        if rowset_b64:
            first_chunk = ArrowResultChunk.from_data(
                rowset_b64,
                arrow_context,
                cursor._use_dict_result,
                cursor._connection._numpy,
                column_names,
                cursor._connection._arrow_number_to_decimal,
            )
        else:
            # Log
            first_chunk = ArrowResultChunk.from_data(
                [],
                arrow_context,
                cursor._use_dict_result,
                cursor._connection._numpy,
                column_names,
                cursor._connection._arrow_number_to_decimal,
            )

    if "chunks" not in data:
        return [first_chunk]
    else:
        chunks = data["chunks"]
        logger.debug("chunk size=%s", len(chunks))
        # prepare the downloader for further fetch
        qrmk = data.get("qrmk")
        chunk_headers: Dict[str, Any] = {}
        if "chunkHeaders" in data:
            chunk_headers = {}
            for header_key, header_value in data["chunkHeaders"].items():
                chunk_headers[header_key] = header_value
                if "encryption" not in header_key:
                    logger.debug(
                        "added chunk header: key=%s, value=%s",
                        header_key,
                        header_value,
                    )
        elif qrmk is not None:
            logger.debug(f"qrmk={qrmk}")
            chunk_headers[SSE_C_ALGORITHM] = SSE_C_AES
            chunk_headers[SSE_C_KEY] = qrmk

        if _format == "json":
            return [first_chunk] + [
                JSONResultChunk(
                    c["rowCount"],
                    chunk_headers,
                    RemoteChunkInfo(
                        url=c["url"],
                        uncompressedSize=c["uncompressedSize"],
                        compressedSize=c["compressedSize"],
                    ),
                    column_names,
                    column_converters,
                    cursor._use_dict_result,
                )
                for c in chunks
            ]
        else:
            return [first_chunk] + [
                ArrowResultChunk(
                    c["rowCount"],
                    chunk_headers,
                    RemoteChunkInfo(
                        url=c["url"],
                        uncompressedSize=c["uncompressedSize"],
                        compressedSize=c["compressedSize"],
                    ),
                    arrow_context,
                    cursor._use_dict_result,
                    cursor._connection._numpy,
                    column_names,
                    cursor._connection._arrow_number_to_decimal,
                )
                for c in chunks
            ]


class ResultChunk(abc.ABC):
    def __init__(
        self,
        rowcount: int,
        chunk_headers: Optional[Dict[str, str]],
        remote_chunk_info: Optional["RemoteChunkInfo"],
        column_names: Sequence[str],
        use_dict_result: bool,
    ):
        self.rowcount = rowcount
        self._chunk_headers = chunk_headers
        self._remote_chunk_info = remote_chunk_info
        self._column_names = column_names
        self._use_dict_result = use_dict_result
        self._metrics: Dict[str, int] = {}
        self._data: Optional[List[List[Any, ...]]] = None

    @property
    def _local(self) -> bool:
        """Whether this chunk is local."""
        return self._data is not None

    @property
    def compressed_size(self) -> Optional[int]:
        """Returns the size of chunk in bytes in compressed form.

        If it's a local chunk this function returns None.
        """
        if self._remote_chunk_info:
            return self._remote_chunk_info.compressedSize
        return None

    @property
    def uncompressed_size(self) -> Optional[int]:
        """Returns the size of chunk in bytes in uncompressed form.

        If it's a local chunk this function returns None.
        """
        if self._remote_chunk_info:
            return self._remote_chunk_info.uncompressedSize
        return None

    def __iter__(
        self,
    ) -> Union[Iterator[Union[Dict, Exception]], Iterator[Union[Tuple, Exception]]]:
        """Returns an iterator through the data this chunk holds.

        In case of this being a local chunk it iterates through the local already parsed
        data and if it's a remote chunk it will download, parse its data and return an
        iterator for it.
        """
        return self._download()

    @abc.abstractmethod
    def _download(
        self, **kwargs
    ) -> Union[Iterator[Union[Dict, Exception]], Iterator[Union[Tuple, Exception]]]:
        raise NotImplementedError()


class JSONResultChunk(ResultChunk):
    def __init__(
        self,
        rowcount: int,
        chunk_headers: Optional[Dict[str, str]],
        remote_chunk_info: Optional["RemoteChunkInfo"],
        column_names: Sequence[str],
        column_converters: Sequence[Tuple[str, "SnowflakeConverterType"]],
        use_dict_result: bool,
    ):
        super().__init__(
            rowcount,
            chunk_headers,
            remote_chunk_info,
            column_names,
            use_dict_result,
        )
        self.column_converters = column_converters

    @classmethod
    def from_data(
        cls,
        data: Sequence[Sequence[Any]],
        column_names: Sequence[str],
        column_converters: Sequence[Tuple[str, "SnowflakeConverterType"]],
        use_dict_result: bool,
    ):
        new_chunk = cls(
            len(data),
            None,
            None,
            column_names,
            column_converters,
            use_dict_result,
        )
        new_chunk._data = list(new_chunk._parse(data))
        return new_chunk

    def _load(self, response):  # TODO types
        with GzipFile(fileobj=response.raw, mode="r") as gfd:
            # Read in decompressed data
            read_data: str = gfd.read().decode("utf-8", "replace")
            return json.loads("".join(["[", read_data, "]"]))

    def _parse(
        self, downloaded_data
    ) -> Union[Iterator[Union[Dict, Exception]], Iterator[Union[Tuple, Exception]]]:
        if self._use_dict_result:
            for row in downloaded_data:
                row_result = {}
                try:
                    for (_t, c), v, n in zip(
                        self.column_converters,
                        row,
                        self._column_names,
                    ):
                        row_result[n] = v if c is None or v is None else c(v)
                except Exception as error:
                    msg = f"Failed to convert: field {n}: {_t}::{v}, Error: {error}"
                    logger.exception(msg)
                    yield Error.errorhandler_make_exception(
                        InterfaceError,
                        {
                            "msg": msg,
                            "errno": ER_FAILED_TO_CONVERT_ROW_TO_PYTHON_TYPE,
                        },
                    )
                yield row_result
        else:
            for row in downloaded_data:
                row_result = [None] * len(self._column_names)
                try:
                    idx = 0
                    for (_t, c), v, _n in zip(
                        self.column_converters,
                        row,
                        self._column_names,
                    ):
                        row_result[idx] = v if c is None or v is None else c(v)
                        idx += 1
                except Exception as error:
                    msg = f"Failed to convert: field {_n}: {_t}::{v}, Error: {error}"
                    logger.exception(msg)
                    yield Error.errorhandler_make_exception(
                        InterfaceError,
                        {
                            "msg": msg,
                            "errno": ER_FAILED_TO_CONVERT_ROW_TO_PYTHON_TYPE,
                        },
                    )
                yield tuple(row_result)

    def __repr__(self) -> str:
        return f"JSONResultChunk({self.rowcount})"

    def _download(
        self, **kwargs
    ) -> Union[Iterator[Union[Dict, Exception]], Iterator[Union[Tuple, Exception]]]:
        if self._local:
            return iter(self._data)
        sleep_timer = 1
        backoff = DecorrelateJitterBackoff(1, 16)
        for retry in range(MAX_DOWNLOAD_RETRY):
            try:
                with TimerContextManager() as download_metric:
                    response = requests.get(
                        self._remote_chunk_info.url,
                        headers=self._chunk_headers,
                        timeout=DOWNLOAD_TIMEOUT,
                        stream=True,  # Default to non-streaming unless arrow
                    )
                    if response.ok:
                        break
            except Exception:
                if retry == MAX_DOWNLOAD_RETRY - 1:
                    # Re-throw if we failed on the last retry
                    raise
                sleep_timer = backoff.next_sleep(1, sleep_timer)
                logger.exception(
                    f"Failed to fetch the large result set chunk "
                    f"{self._remote_chunk_info.url} for the {retry + 1} th time, "
                    f"backing off for {sleep_timer}s"
                )
                time.sleep(sleep_timer)

        self._metrics[
            DownloadMetrics.download.value
        ] = download_metric.get_timing_millis()
        # Load data to a intermediate form
        with TimerContextManager() as load_metric:
            downloaded_data = self._load(response)
        self._metrics[DownloadMetrics.load.value] = load_metric.get_timing_millis()
        # Process downloaded data
        with TimerContextManager() as parse_metric:
            parsed_data = self._parse(downloaded_data)
        self._metrics[DownloadMetrics.parse.value] = parse_metric.get_timing_millis()
        return iter(parsed_data)


class ArrowResultChunk(ResultChunk):
    def __init__(
        self,
        rowcount: int,
        chunk_headers: Optional[Dict[str, str]],
        remote_chunk_info: Optional["RemoteChunkInfo"],
        context: "ArrowConverterContext",
        use_dict_result: bool,
        numpy: bool,
        column_names: Sequence[str],
        number_to_decimal: bool,
    ):
        super().__init__(
            rowcount, chunk_headers, remote_chunk_info, column_names, use_dict_result
        )
        self._context = context
        self._numpy = numpy
        self._number_to_decimal = number_to_decimal

    def __repr__(self) -> str:
        return f"ArrowResultChunk({self.rowcount})"

    def _load(
        self, response, row_unit: str
    ) -> Union[Iterator[Union[Dict, Exception]], Iterator[Union[Tuple, Exception]]]:
        from .arrow_iterator import PyArrowIterator

        gfd = GzipFile(fileobj=response.raw, mode="r")

        iter = PyArrowIterator(
            None,
            gfd,
            self._context,
            self._use_dict_result,
            self._numpy,
            self._number_to_decimal,
        )
        if row_unit == TABLE_UNIT:
            iter.init_table_unit()
        return iter

    def parse(self, downloaded_data):
        return downloaded_data

    def _from_data(self, data: str, iter_unit: int):
        from .arrow_iterator import PyArrowIterator

        if len(data) == 0:
            return iter([])

        _iter = PyArrowIterator(
            None,
            io.BytesIO(b64decode(data)),
            self._context,
            self._use_dict_result,
            self._numpy,
            self._number_to_decimal,
        )
        if iter_unit == TABLE_UNIT:
            _iter.init_table_unit()
        else:
            _iter.init_row_unit()
        return _iter

    @classmethod
    def from_data(
        cls,
        data: Sequence[Sequence[Any]],
        context: "ArrowConverterContext",
        use_dict_result: bool,
        numpy: bool,
        column_names: Sequence[str],
        number_to_decimal: bool,
    ):
        new_chunk = cls(
            len(data),
            None,
            None,
            context,
            use_dict_result,
            numpy,
            column_names,
            number_to_decimal,
        )
        new_chunk._data = data
        return new_chunk

    def _download(
        self, **kwargs
    ) -> Union[Iterator[Union[Dict, Exception]], Iterator[Union[Tuple, Exception]]]:
        iter_unit = kwargs.pop("iter_unit", ROW_UNIT)
        if self._local:
            return self._from_data(self._data, iter_unit)
        sleep_timer = 1
        backoff = DecorrelateJitterBackoff(1, 16)
        for retry in range(MAX_DOWNLOAD_RETRY):
            try:
                with TimerContextManager() as download_metric:
                    response = requests.get(
                        self._remote_chunk_info.url,
                        headers=self._chunk_headers,
                        timeout=DOWNLOAD_TIMEOUT,
                        stream=True,
                    )
                    if response.ok:
                        break
            except Exception:
                if retry == MAX_DOWNLOAD_RETRY - 1:
                    # Re-throw if we failed on the last retry
                    raise
                sleep_timer = backoff.next_sleep(1, sleep_timer)
                logger.exception(
                    f"Failed to fetch the large result set chunk "
                    f"{self._remote_chunk_info.url} for the {retry + 1} th time, "
                    f"backing off for {sleep_timer}s"
                )
                time.sleep(sleep_timer)

        self._metrics[
            DownloadMetrics.download.value
        ] = download_metric.get_timing_millis()
        with TimerContextManager() as load_metric:
            loaded_data = self._load(response, iter_unit)
        self._metrics[DownloadMetrics.load.value] = load_metric.get_timing_millis()
        return loaded_data

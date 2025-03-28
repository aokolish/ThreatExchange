# Copyright (c) Meta Platforms, Inc. and affiliates.

"""
The SignalExchangeAPI talks to external APIs to read/write signals

@see SignalExchangeAPI
"""

import logging

from abc import ABC, abstractmethod
import typing as t

from threatexchange import common
from threatexchange.exchanges.collab_config import CollaborationConfigBase
from threatexchange.signal_type.signal_base import SignalType
from threatexchange.exchanges import fetch_state as state

TCollabConfig = t.TypeVar("TCollabConfig", bound=CollaborationConfigBase)


class SignalExchangeAPI(
    t.Generic[
        TCollabConfig,
        state.TFetchCheckpoint,
        state.TFetchedSignalMetadata,
        state.TUpdateRecordKey,
        state.TUpdateRecordValue,
    ],
    ABC,
):
    """
    APIs to read and maybe write signals.

    This is the key interface that ties together fetching and storing
    hashes, indicators of harm, or other data ("signals"), potentially from
    resources shared by multiple contributors.

    Since these various sources of signals have different APIs and storage
    formats, not only does this interface define how to fetch data, but
    also provides a form that is simplest to iteratively update.

    While this interface is primarily intended for connecting with
    externally hosted servers, it might be useful to write adapters for
    certain formats of local files, which could be valuable for testing.

    Is assumed that fetched signals have some metadata attached to them,
    which is unique to that API. Additionally, it a assumed that there
    might be multiple contributors (owners) to signals inside of an API.


    = fetch_iter() and checkpointing
    SignalExchangeAPIs should checkpoint their progress, so that they
    can tail updates. If this is not possible, they can instead use
    checkpoints to record how long it has been since a full fetch, and
    trigger a refresh if a certain amount of time has passed.

    An instance of this class can retain state (caching connecting, etc)
    as needed.

    = On UpdateRecords & SignalTypes + TFetchedSignalMetadata =
    This class expects that there are two forms that data from the
    exchange will need to be served in:
    1. The "original format" that the API fetches in. This is one unique
       key (TUpdateRecordKey) per value (TUpdateRecordValue) that is
       otherwise opaque to the framework.
    2. The "matching format" that SignalTypeIndex expects. This is one
       unique signal (SignalType, value) per value
       (TFetchedSignalMetadata).

    An implementation of the interface returns format #1 from fetch_iter()
    and converts format #1 to #2 via naive_convert_to_signal_type().

    Why not just store everything in the index format by chaining
    naive_convert_to_signal_type(fetch_iter()) ? Handling updates and
    deletes on the API would be much more complicated if it relies on
    refcounting returns of fetch_iter. Using the same key as the API does
    allows for an implementation that does not need to load the entire
    dataset into memory to merge it. Additionally, it's assumed that there
    is potentially data returned by the API that might not directly translate
    to signals, but be useful to some implementations, and that it would be
    lost if it was translated immediately. For a storage implementation that
    does discard the intermediate TUpdateRecordValue and immediately converts,
    take a look at how HasherMatcherActioner does its storage.

    Some trivial implementations may find that there is not a key that
    makes sense - you can easily use (SignalType+value) as a string key,
    or even just empty string and return the entire copy of the dataset
    in TUpdateRecordValue.

    = On Authentification =
    It's expected that an instance of the class is fully authenticated,
    and in general, auth should be passible by the constructor.

    If an API is constructed via the for_collab classmethod constructor,
    it should attempt to authenticate itself via discovering it from the
    execution environment and passing it via __init__(). If constructed
    directly, it should not search for credentials.
    """

    @classmethod
    @abstractmethod
    def for_collab(cls, collab: TCollabConfig) -> "SignalExchangeAPI":
        """
        An alternative constructor to get a working instance of the API.

        An instance provided by this class is expected to:
        1. Be fully authenticated or throw an exception
        2. All methods are callable if supported by the base implementation

        For implementations that do need additional configuration or
        authentication, @see threatexchange.exchanges.auth
        If not explicitly given auth by those helpers, the implementation
        should attempt to find its own, and throw an exception otherwise.

        Don't:
        * Prompt the user via stdin
        * Return an instance that will throw when any methods are called

        Usages of this library will only attempt to instantiate an API
        when they have an API call to make - all methods for manipulating
        local state are classmethods.
        """
        raise NotImplementedError

    @classmethod
    def get_name(cls) -> str:
        """
        A simple string name unique to SignalExchangeAPIs in use.

        It should be one lowercase_with_underscores word.

        This shouldn't be changed once comitted, or you may break naive
        storage solutions (like the one in the CLI) which stores fetched
        data by (SignalExchangeAPI.name(), collab_name).
        """
        name = cls.__name__
        for suffix in ("API", "Exchange"):
            if name.endswith(suffix):
                name = name[: -len(suffix)]
        return common.class_name_to_human_name(name, "Signal")

    @staticmethod
    @abstractmethod
    def get_checkpoint_cls() -> t.Type[state.TFetchCheckpoint]:
        """Returns the dataclass used to control checkpoint for this API"""
        pass

    @staticmethod
    @abstractmethod
    def get_record_cls() -> t.Type[state.TFetchedSignalMetadata]:
        """Returns the dataclass used to store records for this API"""
        pass

    @staticmethod
    @abstractmethod
    def get_config_cls() -> t.Type[TCollabConfig]:
        """Returns the dataclass used to store records for this API"""
        pass

    @classmethod
    def fetch_value_merge(
        cls,
        old: t.Optional[state.TUpdateRecordValue],
        new: t.Optional[state.TUpdateRecordValue],  # can be modified in-place
    ) -> t.Optional[state.TUpdateRecordValue]:
        """
        Merge a new update produced by fetch.

        Returning a value of None indicates that the entry should be deleted.

        Most implementations will probably prefer the default, which is to
        replace the record entirely.

        It is safe to mutate `new` inline and return it, if needed.
        """
        # Default implementation is replace
        return new

    @classmethod
    @t.final
    def naive_fetch_merge(
        cls,
        old: t.Dict[
            state.TUpdateRecordKey, state.TUpdateRecordValue
        ],  # modified in place
        new: t.Mapping[state.TUpdateRecordKey, t.Optional[state.TUpdateRecordValue]],
    ) -> None:
        """
        Merge the results of a fetch in-memory.

        This implementation is mostly for demonstration purposes and testing,
        since even simple usecases may prefer to avoid loading the whole dataset
        in memory and merging by key.

        For example, if you have nothing else, merging NCMEC update records
        together keyed by ID will eventually get you an entire copy of the database.
        """
        for k, v in new.items():
            new_v = cls.fetch_value_merge(old.get(k), v)
            if new_v is None:
                old.pop(k, None)
            else:
                old[k] = new_v

    # TODO - rename this to make it more clear what it's doing
    #        and consider making it one-key-at-a-time
    @classmethod
    @abstractmethod
    def naive_convert_to_signal_type(
        cls,
        signal_types: t.Sequence[t.Type[SignalType]],
        collab: TCollabConfig,
        fetched: t.Mapping[state.TUpdateRecordKey, state.TUpdateRecordValue],
    ) -> t.Dict[t.Type[SignalType], t.Dict[str, state.TFetchedSignalMetadata]]:
        """
        Convert the record from the API format to the format needed for indexing.

        This is the fallback method of creating state when there isn't a
        specialized storage for the fetch type.

        """
        raise NotImplementedError

    @abstractmethod
    def fetch_iter(
        self,
        supported_signal_types: t.Sequence[t.Type[SignalType]],
        # None if fetching for the first time,
        # otherwise the previous FetchDelta returned
        checkpoint: t.Optional[state.TFetchCheckpoint],
    ) -> t.Iterator[
        state.FetchDelta[
            state.TUpdateRecordKey, state.TUpdateRecordValue, state.TFetchCheckpoint
        ]
    ]:
        """
        Call out to external resources, fetching a batch of updates per yield.

        Many APIs are a sequence of events: (creates/updates, deletions)
        In that case, it's important the these events are strictly ordered.
        I.e. if the sequence is create => delete, if the sequence is reversed
        to delete => create, the end result is a stored record, when the
        expected is a deleted one.

        Updates are assumed to have a keys that can partition the dataset. See the
        note on this in the class docstring.

        The iterator may be abandoned before it is completely exhausted.

        If the iterator returns, it should be because there is no more data
        (i.e. the fetch is up to date).
        """
        raise NotImplementedError

    # TODO - Restore in a future version
    # def report_seen(
    #     self,
    #     collab: TCollabConfig,
    #     s_type: SignalType,
    #     signal: str,
    #     metadata: state.TFetchedSignalMetadata,
    # ) -> None:
    #     """
    #     Report that you observed this signal.

    #     This is an optional API, and places that use it should catch
    #     the NotImplementError.
    #     """
    #     raise NotImplementedError

    def report_opinion(
        self,
        s_type: t.Type[SignalType],
        signal: str,
        opinion: state.SignalOpinion,
    ) -> None:
        """
        Weigh in on a signal for this collaboration.

        Most implementations will want a full replacement specialization, but this
        allows a common interface for all uploads for the simplest usecases.

        This is an optional API, and places that use it should catch
        the NotImplementError.
        """
        raise NotImplementedError


# Convenience for avoiding mypy strict errors
AnySignalExchangeAPI = SignalExchangeAPI[t.Any, t.Any, t.Any, t.Any, t.Any]

# A convenience helper since mypy can't intuit that bound != t.Any
# For methods like get_checkpoint_cls
TSignalExchangeAPI = SignalExchangeAPI[
    CollaborationConfigBase,
    state.FetchCheckpointBase,
    state.FetchedSignalMetadata,
    t.Any,
    t.Any,
]

TSignalExchangeAPICls = t.Type[TSignalExchangeAPI]


class SignalExchangeAPIWithSimpleUpdates(
    SignalExchangeAPI[
        TCollabConfig,
        state.TFetchCheckpoint,
        state.TFetchedSignalMetadata,
        t.Tuple[str, str],
        state.TFetchedSignalMetadata,
    ],
):
    """
    An API that conveniently maps directly into the form needed by index.

    If the API supports returning exactly the hashes and all the metadata needed
    to make a decision on the hash without needing an indirection of ID (for example,
    to support deletes), then you can choose to directly return it in a form that
    maps directly into SignalType.
    """

    @classmethod
    def naive_convert_to_signal_type(
        cls,
        signal_types: t.Sequence[t.Type[SignalType]],
        collab: TCollabConfig,
        fetched: t.Mapping[t.Tuple[str, str], t.Optional[state.TFetchedSignalMetadata]],
    ) -> t.Dict[t.Type[SignalType], t.Dict[str, state.TFetchedSignalMetadata]]:
        ret: t.Dict[t.Type[SignalType], t.Dict[str, state.TFetchedSignalMetadata]] = {}
        type_by_name = {st.get_name(): st for st in signal_types}
        for (type_str, signal_str), metadata in fetched.items():
            s_type = type_by_name.get(type_str)
            if s_type is None or metadata is None:
                continue
            try:
                s_type.validate_signal_str(signal_str)
            except ValueError:
                logging.warning(
                    "Invalid fingerprint (%s): %s",
                    s_type.get_name(),
                    signal_str if len(signal_str) < 100 else signal_str[:100] + "...",
                )
                continue
            inner = ret.get(s_type)
            if inner is None:
                inner = {}
                ret[s_type] = inner
            inner[signal_str] = metadata
        return ret

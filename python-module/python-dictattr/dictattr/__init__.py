#!/usr/bin/env python3
# encoding: utf-8

from __future__ import annotations

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__version__ = (0, 0, 2)
__all__ = [
    "odict", "AttrDict", "MapAttr", "MuMapAttr", 
    "DictAttr", "ChainDictAttr", "UserDictAttr", 
]

from collections import UserDict
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Generic, Self, TypeVar


K = TypeVar("K")
V = TypeVar("V")


class odict(dict[K, V]):

    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__ # type: ignore
    __delattr__ = dict.__delitem__ # type: ignore


class AttrDict(dict[K, V]):

    def __init__(self, /, *args, **kwds):
        super().__init__(*args, **kwds)
        self.__dict__ = self # type: ignore


@Mapping.register
class MapAttr(Generic[K, V]):

    def __init__(self, d: None | dict = None, /):
        self.__dict__: dict[K, V] # type: ignore
        if d is not None:
            self.__dict__ = d

    def __contains__(self, key, /) -> bool:
        return key in self.__dict__

    def __getitem__(self, key, /) -> V:
        return self.__dict__[key]

    def __iter__(self, /) -> Iterator[K]:
        return iter(self.__dict__)

    def __len__(self, /) -> int:
        return len(self.__dict__)

    def __repr__(self, /) -> str:
        cls = type(self)
        if (mod := cls.__module__) == "__main__":
            return f"{cls.__qualname__}({self.__dict__})"
        else:
            return f"{mod}.{cls.__qualname__}({self.__dict__})"

    @classmethod
    def of(cls, /, *args, **kwds) -> Self:
        return cls(dict(*args, **kwds))


@MutableMapping.register
class MuMapAttr(MapAttr[K, V]):

    def __delitem__(self, key, /):
        del self.__dict__[key]

    def __setitem__(self, key: K, val: V, /):
        self.__dict__[key] = val


class DictAttr(MuMapAttr[K, V]):

    def __getattr__(self, attr, /):
        return getattr(self.__dict__, attr)

    def __getattribute__(self, attr, /):
        if attr == "__dict__":
            return super().__getattribute__(attr)
        try:
            return self[attr]
        except KeyError:
            return super().__getattribute__(attr)

    def __getitem__(self, key, /) -> V | Self: # type: ignore
        d = self.__dict__[key]
        if type(d) is dict:
            return type(self)(d)
        return d


class ChainDictAttr(DictAttr[K, V | "ChainDictAttr"]):

    def __getitem__(self, key, /) -> V | ChainDictAttr:
        try:
            return super().__getitem__(key)
        except KeyError:
            d = self.__dict__[key] = type(self)()
            return d


class UserDictAttr(UserDict[K, V]):

    @classmethod
    def of(cls, m: Mapping, /) -> Self:
        self = cls()
        self.__dict__["data"] = m # type: ignore
        return self

    def __delattr__(self, attr, /):
        try:
            del self[attr]
        except KeyError:
            try:
                super().__delattr__(attr)
            except KeyError:
                raise AttributeError(attr)

    def __getattr__(self, attr, /):
        return getattr(self.data, attr)

    def __getattribute__(self, attr, /):
        if attr in ("__dict__", "data") or attr == f"__{attr.strip('_')}__":
            return super().__getattribute__(attr)
        try:
            return self[attr]
        except KeyError:
            return super().__getattribute__(attr)

    def __getitem__(self, key, /) -> V | Self: # type: ignore
        d = super().__getitem__(key)
        if type(d) is dict:
            return type(self)(d)
        return d

    def __setattr__(self, attr, val, /):
        if attr == "data" and "data" not in self.__dict__:
            self.__dict__["data"] = val
        else:
            self[attr] = val


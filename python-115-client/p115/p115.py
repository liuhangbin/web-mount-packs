#!/usr/bin/env python3
# encoding: utf-8

from __future__ import annotations

__author__ = "ChenyangGao <https://chenyanggao.github.io>"
__all__ = [
    "P115Client", "P115Path", "P115FileSystem", "P115SharePath", "P115ShareFileSystem", 
    "P115ZipPath", "P115ZipFileSystem", "P115Offline", 
]

import errno

from base64 import b64encode
from binascii import b2a_hex
from collections import deque
from copy import deepcopy
from datetime import datetime
from functools import cached_property
from hashlib import md5, sha1
from io import BufferedReader, BytesIO, TextIOWrapper, UnsupportedOperation, DEFAULT_BUFFER_SIZE
from json import dumps, loads
from os import fsdecode, fstat, stat, PathLike
from os import path as os_path
from posixpath import dirname, join as joinpath, normpath
from re import compile as re_compile
from time import time
from typing import Callable, Final, Iterable, Iterator, Optional, Sequence
from types import MappingProxyType
from urllib.parse import parse_qsl, urlencode, urlparse
from uuid import uuid4

from requests.cookies import create_cookie
from requests.exceptions import Timeout
from requests.models import Response
from requests.sessions import Session

import oss2 # TODO: 以后会去除这个依赖，自己实现对上传接口的调用，以支持异步
import qrcode # OR use `pyqrcode` instead

from .exception import AuthenticationError, BadRequest, LoginError
from .util.cipher import P115RSACipher, P115ECDHCipher, MD5_SALT
from .util.file import get_filesize, RequestsFileReader
from .util.hash import file_digest
from .util.iter import cut_iter


ID_OR_PATH_TYPE = int | str | Sequence[str]
CRE_SHARE_LINK = re_compile(r"/s/(?P<share_code>\w+)(\?password=(?P<receive_code>\w+))?")
APP_VERSION: Final = "99.99.99.99"
rsa_encoder = P115RSACipher()
ecdh_encoder = P115ECDHCipher()

Response.__del__ = Response.close


def check_get(resp, exc_cls=BadRequest):
    if resp["state"]:
        return resp.get("data")
    raise exc_cls(resp)


def text_to_dict(s, /, entry_sep="\n", kv_sep="="):
    return dict(
        map(str.strip, e.split(kv_sep, 1)) 
        for e in s.strip(entry_sep).split(entry_sep)
    )


def console_qrcode(text):
    qr = qrcode.QRCode(border=1)
    qr.add_data(text)
    qr.print_ascii(tty=True)


def normalize_info(info, keep_raw=False):
    if "fid" in info:
        fid = info["fid"]
        parent_id = info["cid"]
        is_dir = False
    else:
        fid = info["cid"]
        parent_id = info["pid"]
        is_dir = True
    info2 =  {
        "name": info["n"], 
        "is_dir": is_dir, 
        "size": info.get("s"), 
        "id": int(fid), 
        "parent_id": int(parent_id), 
        "sha1": info.get("sha"), 
    }
    if "te" in info:
        info2.update({
            "etime": datetime.fromtimestamp(int(info["te"])), 
            "utime": datetime.fromtimestamp(int(info["tu"])), 
            "ptime": datetime.fromtimestamp(int(info["tp"])), 
            "open_time": datetime.fromtimestamp(int(info["to"])), 
        })
    elif "t" in info:
        info2["time"] = datetime.fromtimestamp(int(info["t"]))
    if "pc" in info:
        info2["pick_code"] = info["pc"]
    if "m" in info:
        info2["star"] = bool(info["m"])
    if keep_raw:
        info2["raw"] = info
    return info2


class P115Client:

    def __init__(self, /, cookie=None):
        self._session = session = Session()
        session.headers["User-Agent"] = f"Mozilla/5.0 115disk/{APP_VERSION}"
        if not cookie:
            cookie = self.login_with_qrcode()["data"]["cookie"]
        self.cookie = cookie
        resp = self.upload_info()
        if resp["errno"]:
            raise AuthenticationError(resp)
        self.user_id = str(resp["user_id"])
        self.user_key = resp["userkey"]

    def __del__(self, /):
        self.close()

    def close(self, /):
        self._session.close()

    @property
    def cookie(self, /) -> str:
        return self._cookie

    @cookie.setter
    def cookie(self, cookie, /):
        if isinstance(cookie, str):
            cookie = text_to_dict(cookie, entry_sep=";")
        cookiejar = self._session.cookies
        cookiejar.clear()
        if isinstance(cookie, dict):
            for key in ("UID", "CID", "SEID"):
                cookiejar.set_cookie(
                    create_cookie(key, cookie[key], domain=".115.com", rest={'HttpOnly': True})
                )
        else:
            cookiejar.update(cookie)
        cookies = cookiejar.get_dict()
        self._cookie = "; ".join(f"{key}={cookies[key]}" for key in ("UID", "CID", "SEID"))

    @property
    def session(self, /):
        return self._session

    def login_with_qrcode(self, /, **request_kwargs):
        """用二维码登录
        """
        qrcode_token = self.login_qrcode_token(**request_kwargs)["data"]
        qrcode = qrcode_token.pop("qrcode")
        console_qrcode(qrcode)
        while True:
            try:
                resp = self.login_qrcode_status(qrcode_token, **request_kwargs)
            except Timeout:
                continue
            status = resp["data"].get("status")
            if status == 0:
                print("[status=0] qrcode: waiting")
            elif status == 1:
                print("[status=1] qrcode: scanned")
            elif status == 2:
                print("[status=2] qrcode: signed in")
                break
            elif status == -1:
                raise LoginError("[status=-1] qrcode: expired")
            elif status == -2:
                raise LoginError("[status=-2] qrcode: canceled")
            else:
                raise LoginError(f"qrcode: aborted with {resp!r}")
        return self.login_qrcode_result({"account": qrcode_token["uid"]}, **request_kwargs)

    def request(self, api, /, method="GET", *, parse=False, **request_kwargs):
        """
        """
        request_kwargs["stream"] = True
        resp = self._session.request(method, api, **request_kwargs)
        resp.raise_for_status()
        if callable(parse):
            return parse(resp.content)
        if parse:
            if request_kwargs.get("stream"):
                return resp
            else:
                content_type = resp.headers.get("Content-Type", "")
                if content_type == "application/json" or content_type.startswith("application/json;"):
                    return resp.json()
                elif content_type.startswith("text/"):
                    return resp.text
                return resp.content
        return resp

    ########## Version API ##########

    @staticmethod
    def list_app_version(**request_kwargs):
        """获取当前各平台最新版 115 app
        GET https://appversion.115.com/1/web/1.0/api/chrome
        """
        api = "https://appversion.115.com/1/web/1.0/api/chrome"
        return Session().get(api, **request_kwargs).json()

    ########## Account API ##########

    def login_check(self, /, **request_kwargs):
        """检查当前用户的登录状态（用处不大）
        GET http://passportapi.115.com/app/1.0/web/1.0/check/sso/
        """
        api = "http://passportapi.115.com/app/1.0/web/1.0/check/sso/"
        return self.request(api, parse=loads, **request_kwargs)

    def login_qrcode_status(self, /, payload: dict, **request_kwargs):
        """获取二维码的状态（未扫描、已扫描、已登录、已取消、已过期等），payload 数据取自 `login_qrcode_token` 接口响应
        GET https://qrcodeapi.115.com/get/status/
        payload:
            - uid: str
            - time: int
            - sign: str
        """
        api = "https://qrcodeapi.115.com/get/status/"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def login_qrcode_result(self, /, payload: dict, **request_kwargs):
        """获取扫码登录的结果，包含 cookie
        POST https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/
        payload:
            - account: int | str
            - app: str = "web"
        """
        api = "https://passportapi.115.com/app/1.0/web/1.0/login/qrcode/"
        return self.request(api, "POST", data={"app": "web", **payload}, parse=loads, **request_kwargs)

    def login_qrcode_token(self, /, **request_kwargs):
        """获取登录二维码，扫码可用
        GET https://qrcodeapi.115.com/api/1.0/web/1.0/token/
        """
        api = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
        return self.request(api, parse=loads, **request_kwargs)

    def logout(self, /, **request_kwargs):
        """退出登录状态（如无必要，不要使用）
        GET https://passportapi.115.com/app/1.0/web/1.0/logout/logout/
        """
        api = "https://passportapi.115.com/app/1.0/web/1.0/logout/logout/"
        self.request(api, **request_kwargs)

    def login_status(self, /, **request_kwargs):
        """获取登录状态
        GET https://my.115.com/?ct=guide&ac=status
        """
        api = "https://my.115.com/?ct=guide&ac=status"
        return self.request(api, parse=loads, **request_kwargs)

    def user_info(self, /, **request_kwargs):
        """获取此用户信息
        GET https://my.115.com/?ct=ajax&ac=na
        """
        api = "https://my.115.com/?ct=ajax&ac=nav"
        return self.request(api, parse=loads, **request_kwargs)

    def user_info2(self, /, **request_kwargs):
        """获取此用户信息（更全）
        GET https://my.115.com/?ct=ajax&ac=get_user_aq
        """
        api = "https://my.115.com/?ct=ajax&ac=get_user_aq"
        return self.request(api, parse=loads, **request_kwargs)

    def user_setting(self, payload: dict = {}, /, **request_kwargs):
        """获取（并可修改）此账户的网页版设置
        POST https://115.com/?ac=setting&even=saveedit&is_wl_tpl=1
        """
        api = "https://115.com/?ac=setting&even=saveedit&is_wl_tpl=1"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    ########## File System API ##########

    def fs_batch_copy(self, /, payload, **request_kwargs):
        """复制文件或文件夹
        POST https://webapi.115.com/files/copy
        payload:
            - pid: int | str
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/files/copy"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def fs_batch_delete(self, payload: dict, /, **request_kwargs):
        """删除文件或文件夹
        POST https://webapi.115.com/rb/delete
        payload:
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/rb/delete"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def fs_batch_move(self, payload: dict, /, **request_kwargs):
        """移动文件或文件夹
        POST https://webapi.115.com/files/move
        payload:
            - pid: int | str
            - fid[0]: int | str
            - fid[1]: int | str
            - ...
        """
        api = "https://webapi.115.com/files/move"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def fs_batch_rename(self, payload: dict, /, **request_kwargs):
        """重命名文件或文件夹
        POST https://webapi.115.com/files/batch_rename
        payload:
            - files_new_name[{file_id}]: str # 值为新的文件名（basename）
        """
        api = "https://webapi.115.com/files/batch_rename"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def fs_copy(self, /, fids, pid, **request_kwargs):
        """复制文件或文件夹，此接口是对 `fs_batch_copy` 的封装
        """
        if isinstance(fids, (int, str)):
            data = {"fid[0]": fids}
        else:
            data = {f"fid[{fid}]": fid for i, fid in enumerate(fids)}
            if not data:
                return
        data["pid"] = pid
        return self.fs_batch_copy(payload, **request_kwargs)

    def fs_delete(self, fids, /, **request_kwargs):
        """删除文件或文件夹，此接口是对 `fs_batch_delete` 的封装
        """
        api = "https://webapi.115.com/rb/delete"
        if isinstance(fids, (int, str)):
            data = {"fid[0]": fids}
        else:
            data = {f"fid[{i}]": fid for i, fid in enumerate(fids)}
            if not data:
                return
        return self.fs_batch_delete(payload, **request_kwargs)

    def fs_file(self, payload: dict, /, **request_kwargs):
        """获取文件或文件夹的简略信息
        GET https://webapi.115.com/files/file
        payload:
            file_id: int | str
        """
        api = "https://webapi.115.com/files/file"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def fs_files(self, payload: dict = {}, /, **request_kwargs):
        """获取文件夹的中的文件列表和基本信息
        GET https://webapi.115.com/files
        payload:
            - cid: int | str = 0
            - limit: int = 30
            - offset: int = 0
            - show_dir: 0 | 1 | "" = 1

            - aid: int | str = 1
            - asc: 0 | 1 | "" = 1
            - code: int | str = ""
            - count_folders: 0 | 1 | "" = ""
            - custom_order: int | str = ""
            - fc_mix: 0 | 1 | "" = ""
            - format: str = "json"
            - is_q: 0 | 1 | "" = ""
            - is_share: 0 | 1 | "" = ""
            - natsort: 0 | 1 | "" = ""
            - o: str = "file_name"
            - record_open_time: 0 | 1 | "" = ""
            - scid: int | str = ""
            - snap: 0 | 1 | "" = ""
            - star: 0 | 1 | "" = ""
            - source: str = ""
            - suffix: str = ""
            - type: str = ""
        """
        api = "https://webapi.115.com/files"
        payload = {"cid": 0, "limit": 30, "offset": 0, "show_dir": 1, **payload}
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def fs_files2(self, payload: dict = {}, /, **request_kwargs):
        """获取文件夹的中的文件列表和基本信息
        GET https://aps.115.com/natsort/files.php
        payload:
            - cid: int | str = 0
            - limit: int = 30
            - offset: int = 0
            - show_dir: 0 | 1 | "" = 1

            - aid: int | str = 1
            - asc: 0 | 1 | "" = 1
            - code: int | str = ""
            - count_folders: 0 | 1 | "" = ""
            - custom_order: int | str = ""
            - fc_mix: 0 | 1 | "" = ""
            - format: str = "json"
            - is_q: 0 | 1 | "" = ""
            - is_share: 0 | 1 | "" = ""
            - natsort: 0 | 1 | "" = ""
            - o: str = "file_name"
            - record_open_time: 0 | 1 | "" = ""
            - scid: int | str = ""
            - snap: 0 | 1 | "" = ""
            - star: 0 | 1 | "" = ""
            - source: str = ""
            - suffix: str = ""
            - type: str = ""
        """
        api = "https://aps.115.com/natsort/files.php"
        payload = {"cid": 0, "limit": 30, "offset": 0, "show_dir": 1, **payload}
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def fs_statistic(self, payload: dict, /, **request_kwargs):
        """获取文件或文件夹的统计信息
        GET https://webapi.115.com/category/get
        payload:
            cid: int | str
            aid: int | str = 1
        """
        api = "https://webapi.115.com/category/get"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def fs_get_repeat(self, payload: dict, /, **request_kwargs):
        """文件查重
        GET https://webapi.115.com/files/get_repeat_sha
        payload:
            file_id: int | str
        """
        api = "https://webapi.115.com/files/get_repeat_sha"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def fs_info(self, payload: dict, /, **request_kwargs):
        """获取文件或文件夹的基本信息
        GET https://webapi.115.com/files/get_info
        payload:
            file_id: int | str
        """
        api = "https://webapi.115.com/files/get_info"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def fs_mkdir(self, payload: dict, /, **request_kwargs):
        """新建文件夹
        POST https://webapi.115.com/files/add
        payload:
            cname: str
            pid: int | str = 0
        """
        api = "https://webapi.115.com/files/add"
        payload = {"pid": 0, **payload}
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def fs_move(self, fids, pid, /, **request_kwargs):
        """移动文件或文件夹，此接口是对 `fs_batch_move` 的封装
        """
        payload = {f"fid[{i}]": fid for i, fid in enumerate(fids)}
        if not payload:
            return
        payload["pid"] = pid
        return self.fs_batch_move(payload, **request_kwargs)

    def fs_rename(self, fid_name_pairs, /, **request_kwargs):
        """重命名文件或文件夹，此接口是对 `fs_batch_rename` 的封装
        """
        payload = {f"files_new_name[{fid}]": name for fid, name in fid_name_pairs}
        return self.fs_batch_rename(payload, **request_kwargs)

    def fs_search(self, payload: dict, /, **request_kwargs):
        """搜索文件或文件夹
        GET https://webapi.115.com/files/search
        payload:
            - search_value: str
            - aid: int | str = 1
            - cid: int | str = 0
            - count_folders: int = 1
            - date: str = ""
            - format: str = "json"
            - limit: int = 100
            - offset int = 0
            - pick_code: str = ""
            - source: str = ""
            - type: str = ""
        """
        api = "https://webapi.115.com/files/search"
        payload = {"aid": 1, "cid": 0, "count_folders": 1, "format": "json", "limit": 100, **payload}
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def comment_get(self, /, payload: dict, **request_kwargs):
        """获取文件或文件夹的备注
        GET https://webapi.115.com/files/desc
        payload:
            - file_id: int | str
            - format: str = "json"
            - compat: 0 | 1 = 1
            - new_html: 0 | 1 = 1
        """
        api = "https://webapi.115.com/files/desc"
        payload = {"format": "json", "compat": 1, "new_html": 1, **payload}
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def comment_set(self, /, payload: dict, **request_kwargs):
        """为文件或文件夹设置备注
        POST https://webapi.115.com/files/edit
        payload:
            - fid: int | str
            - file_desc: str = ""
        """
        api = "https://webapi.115.com/files/edit"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def star_set(self, payload: dict, **request_kwargs):
        """为文件或文件夹设置星标
        POST https://webapi.115.com/files/star
        payload:
            - file_id: int | str
            - star: 0 | 1 = 1
        """
        api = "https://webapi.115.com/files/star"
        payload = {"star": 1, **payload}
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    ########## Share API ##########

    def share_snap(self, payload: dict, /, **request_kwargs):
        """获取分享链接的某个文件夹中的文件和子文件夹的列表（包含详细信息）
        GET https://webapi.115.com/share/snap
        payload:
            - share_code: str
            - receive_code: str
            - cid: int | str = 0
            - limit: int = 30
            - offset int = 0
        }
        """
        api = "https://webapi.115.com/share/snap"
        payload = {"cid": 0, "limit": 30, "offset": 0, **payload}
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def share_downlist(self, payload: dict, /, **request_kwargs):
        """获取分享链接的某个文件夹中可下载的文件的列表（只含文件，不含文件夹，任意深度，简略信息）
        GET https://proapi.115.com/app/share/downlist
        payload:
            - share_code: str
            - receive_code: str
            - cid: int | str = 0
        """
        api = "https://proapi.115.com/app/share/downlist"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def share_receive(self, payload: dict, /, **request_kwargs):
        """接收分享链接的某些文件或文件夹
        POST https://webapi.115.com/share/receive
        payload:
            - share_code: str
            - receive_code: str
            - file_id: int | str             # 有多个时，用逗号,分隔
            - cid: int | str = 0             # 这是你网盘的文件夹 cid
            - user_id: int | str = <default> # 有默认值，可以不传
        """
        api = "https://webapi.115.com/share/receive"
        payload = {"cid": 0, "user_id": self.user_id, **payload}
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def share_download_url_web(self, payload: dict, /, **request_kwargs):
        """获取分享链接中某个文件的下载链接（网页版接口，不推荐使用）
        GET https://webapi.115.com/share/downurl
        payload:
            - file_id: int | str
            - receive_code: str
            - share_code: str
            - user_id: int | str = <default> # 有默认值，可以不传
        """
        api = "https://webapi.115.com/share/downurl"
        payload = {"user_id": self.user_id, **payload}
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def share_download_url_app(self, payload: dict, /, **request_kwargs):
        """获取分享链接中某个文件的下载链接
        POST https://proapi.115.com/app/share/downurl
        payload:
            - file_id: int | str
            - receive_code: str
            - share_code: str
            - user_id: int | str = <default> # 有默认值，可以不传
        """
        api = "https://proapi.115.com/app/share/downurl"
        def parse(content):
            resp = loads(content)
            if resp["state"]:
                resp["data"] = loads(rsa_encoder.decode(resp["data"]))
            return resp
        data = rsa_encoder.encode(dumps({"user_id": self.user_id, **payload}))
        return self.request(api, "POST", data={"data": data}, parse=parse, **request_kwargs)

    def share_download_url(self, payload: dict, /, **request_kwargs):
        """获取分享链接中某个文件的下载链接，此接口是对 `share_download_url_app` 的封装
        POST https://proapi.115.com/app/share/downurl
        payload:
            - file_id: int | str
            - receive_code: str
            - share_code: str
            - user_id: int | str = <default> # 有默认值，可以不传
        """
        resp = self.share_download_url_app(payload, **request_kwargs)
        if not resp["state"]:
            raise ValueError(resp)
        return resp["data"]["url"]["url"]

    ########## Download API ##########

    def download_url_web(self, payload: dict, /, **request_kwargs):
        """获取文件的下载链接（网页版接口，不推荐使用）
        GET https://webapi.115.com/files/download
        payload:
            - pickcode: str
        """
        api = "https://webapi.115.com/files/download"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def download_url_app(self, payload: dict, /, **request_kwargs):
        """获取文件的下载链接
        POST https://proapi.115.com/app/chrome/downurl
        payload:
            - pickcode: str
        """
        api = "https://proapi.115.com/app/chrome/downurl"
        def parse(content):
            resp = loads(content)
            if resp["state"]:
                resp["data"] = loads(rsa_encoder.decode(resp["data"]))
            return resp
        data = rsa_encoder.encode(dumps(payload))
        return self.request(api, "POST", data={"data": data}, parse=parse, **request_kwargs)

    def download_url(self, pick_code: str, /, **request_kwargs):
        """获取文件的下载链接，此接口是对 `download_url_app` 的封装
        """
        resp = self.download_url_app({"pickcode": pick_code}, **request_kwargs)
        if not resp["state"]:
            raise ValueError(resp)
        return next(iter(resp["data"].values()))["url"]["url"]

    ########## Upload API ##########

    def upload_info(self, /, **request_kwargs):
        """获取和上传有关的各种服务信息
        GET https://proapi.115.com/app/uploadinfo
        """
        api = "https://proapi.115.com/app/uploadinfo"
        return self.request(api, parse=loads, **request_kwargs)

    def upload_url(self, /, **request_kwargs):
        """获取用于上传的一些 http 接口，此接口具有一定幂等性，请求一次，然后把响应记下来即可
        GET https://uplb.115.com/3.0/getuploadinfo.php
        response:
            - endpoint: 此接口用于上传文件到阿里云 OSS 
            - gettokenurl: 上传前需要用此接口获取 token
        """
        api = "https://uplb.115.com/3.0/getuploadinfo.php"
        return self.request(api, parse=loads, **request_kwargs)

    def upload_sample_init(self, /, payload: dict, **request_kwargs):
        """网页端的上传接口，并不能秒传
        POST https://uplb.115.com/3.0/sampleinitupload.php
        payload:
            - userid: int | str
            - filename: str
            - filesize: int
            - target: str = "U_1_0"
        """
        api = "https://uplb.115.com/3.0/sampleinitupload.php"
        payload = {"target": "U_1_0", **payload}
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def upload_file_sample(self, /, file, filename=None, pid=0, filesize=-1, **request_kwargs):
        """基于 `upload_sample_init` 的上传接口
        """
        if hasattr(file, "read"):
            if isinstance(file, TextIOWrapper):
                file = file.buffer
            if not filename:
                try:
                    filename = os_path.basename(fsdecode(file.name))
                except Exception:
                    filename = str(uuid4())
            if filesize < 0:
                filesize = get_filesize(file)
                if file.tell() != 0:
                    file.seek(0)
        else:
            if not filename:
                filename = os_path.basename(fsdecode(file))
            if filesize < 0:
                filesize = get_filesize(file)
            file = open(file, "rb", buffering=0)
        payload = {
            "userid": self.user_id, 
            "filename": filename, 
            "filesize": filesize, 
            "target": f"U_1_{pid}", 
        }
        resp = self.upload_sample_init(payload, **request_kwargs)
        api = resp["host"]
        payload = {
            "name": payload["filename"], 
            "target": payload["target"], 
            "key": resp["object"], 
            "policy": resp["policy"], 
            "OSSAccessKeyId": resp["accessid"], 
            "success_action_status": 200, 
            "callback": resp["callback"], 
            "signature": resp["signature"], 
        }
        return self.request(api, "POST", data=payload, parse=loads, files={"file": file}, **request_kwargs)

    def upload_init(self, /, **request_kwargs):
        """秒传接口，参数的构造较为复杂，所以请不要直接使用
        POST https://uplb.115.com/4.0/initupload.php
        """
        api = "https://uplb.115.com/4.0/initupload.php"
        return self.request(api, "POST", **request_kwargs)

    def upload_sha1(self, /, filename, filesize, file_sha1, target, sign_key="", sign_val="", **request_kwargs):
        """秒传接口，此接口是对 `upload_init` 的封装，但不建议直接使用
        POST https://uplb.115.com/4.0/initupload.php
        """
        def gen_sig():
            sig_sha1 = sha1()
            sig_sha1.update(bytes(userkey, "ascii"))
            sig_sha1.update(b2a_hex(sha1(bytes(f"{userid}{file_sha1}{target}0", "ascii")).digest()))
            sig_sha1.update(b"000000")
            return sig_sha1.hexdigest().upper()
        def gen_token():
            token_md5 = md5(MD5_SALT)
            token_md5.update(bytes(f"{file_sha1}{filesize}{sign_key}{sign_val}{userid}{t}", "ascii"))
            token_md5.update(b2a_hex(md5(bytes(userid, "ascii")).digest()))
            token_md5.update(bytes(APP_VERSION, "ascii"))
            return token_md5.hexdigest()
        userid, userkey = self.user_id, self.user_key
        t = int(time())
        sig = gen_sig()
        token = gen_token()
        encoded_token = ecdh_encoder.encode_token(t).decode("ascii")
        params = {"k_ec": encoded_token}
        data = {
            "appid": 0, 
            "appversion": APP_VERSION, 
            "userid": userid, 
            "filename": filename, 
            "filesize": filesize, 
            "fileid": file_sha1, 
            "target": target, 
            "sig": sig, 
            "t": t, 
            "token": token, 
        }
        if sign_key and sign_val:
            data["sign_key"] = sign_key
            data["sign_val"] = sign_val
        encrypted = ecdh_encoder.encode(urlencode(sorted(data.items())))
        return self.upload_init(
            params=params, 
            data=encrypted, 
            parse=lambda content: loads(ecdh_encoder.decode(content)), 
            headers={"Content-Type": "application/x-www-form-urlencoded"}, 
            **request_kwargs, 
        )

    def upload_file_sha1_simple(self, /, filename, filesize, file_sha1, read_bytes_range, pid=0):
        """秒传接口，此接口是对 `upload_sha1` 的封装，推荐使用
        """
        fileinfo = {"filename": filename, "filesize": filesize, "file_sha1": file_sha1.upper(), "target": f"U_1_{pid}"}
        resp = self.upload_sha1(**fileinfo)
        if resp["status"] == 7 and resp["statuscode"] == 701:
            sign_key = resp["sign_key"]
            sign_check = resp["sign_check"]
            data = read_bytes_range(sign_check)
            fileinfo["sign_key"] = sign_key
            fileinfo["sign_val"] = sha1(data).hexdigest().upper()
            resp = self.upload_sha1(**fileinfo)
            fileinfo["sign_check"] = sign_check
        resp["fileinfo"] = fileinfo
        return resp

    def upload_file_sha1(self, /, file, filename=None, pid=0, filesize=-1, file_sha1=None):
        """秒传接口，此接口是对 `upload_sha1` 的封装，推荐使用
        """
        if hasattr(file, "read"):
            if isinstance(file, TextIOWrapper):
                file = file.buffer
            if not filename:
                try:
                    filename = os_path.basename(fsdecode(file.name))
                except Exception:
                    filename = str(uuid4())
            if not file_sha1:
                filesize, sha1obj = file_digest(file, "sha1")
                file_sha1 = sha1obj.hexdigest()
            if filesize < 0:
                filesize = get_filesize(file)
        else:
            if not filename:
                filename = os_path.basename(fsdecode(file))
            if not file_sha1:
                filesize, sha1obj = file_digest(open(file, "rb"), "sha1")
                file_sha1 = sha1obj.hexdigest()
            if filesize < 0:
                filesize = get_filesize(file)
        fileinfo = {"filename": filename, "filesize": filesize, "file_sha1": file_sha1.upper(), "target": f"U_1_{pid}"}
        resp = self.upload_sha1(**fileinfo)
        if resp["status"] == 7 and resp["statuscode"] == 701:
            sign_key = resp["sign_key"]
            sign_check = resp["sign_check"]
            if not hasattr(file, "read"):
                file = open(file, "rb")
            start, end = map(int, sign_check.split("-"))
            file.seek(start)
            fileinfo["sign_key"] = sign_key
            fileinfo["sign_val"] = sha1(file.read(end-start+1)).hexdigest().upper()
            resp = self.upload_sha1(**fileinfo)
            fileinfo["sign_check"] = sign_check
        resp["fileinfo"] = fileinfo
        return resp

    # TODO: 增加断点续传机制
    def upload_file(
        self, 
        /, 
        file, 
        filename=None, 
        pid=0, 
        filesize=-1, 
        file_sha1=None, 
        progress_callback=None, 
        multipart_threshold=None, 
    ):
        """基于 `upload_file_sha1` 的上传接口，是高层封装，推荐使用
        """
        resp = self.upload_file_sha1(file, filename, pid, filesize=filesize, file_sha1=file_sha1)
        if resp["status"] == 2 and resp.get("statuscode", 0) == 0:
            return resp
        elif resp["status"] == 1 and resp.get("statuscode", 0) == 0:
            bucket_name, key, callback = resp["bucket"], resp["object"], resp["callback"]
        else:
            raise ValueError(resp)
        if hasattr(file, "read"):
            if isinstance(file, TextIOWrapper):
                file = file.buffer
            if file.tell() != 0:
                file.seek(0)
        else:
            file = open(file, "rb")
        multipart_threshold = multipart_threshold or oss2.defaults.multipart_threshold
        if filesize <= multipart_threshold:
            upload_resp = self._oss_upload(
                bucket_name, 
                key, 
                file, 
                callback, 
                progress_callback=progress_callback, 
            )
        else:
            upload_resp = self._oss_multipart_upload(
                bucket_name, 
                key, 
                file, 
                callback, 
                progress_callback=progress_callback, 
                total_size=resp["fileinfo"]["filesize"], 
            )
        resp["upload"] = upload_resp
        return resp

    def _oss_upload(
        self, 
        /, 
        bucket_name, 
        key, 
        file, 
        callback, 
        progress_callback=None, 
    ):
        """帮助函数：上传文件到阿里云 OSS，一次上传全部
        """
        uploadinfo = self.upload_url()
        token = self.request(uploadinfo["gettokenurl"], parse=loads)
        auth = oss2.Auth(token["AccessKeyId"], token["AccessKeySecret"])
        bucket = oss2.Bucket(auth, uploadinfo["endpoint"], bucket_name)
        headers={
            "User-Agent": "aliyun-sdk-android/2.9.1", 
            "x-oss-security-token": token["SecurityToken"], 
            "x-oss-callback": b64encode(bytes(callback["callback"], "ascii")).decode("ascii"),
            "x-oss-callback-var": b64encode(bytes(callback["callback_var"], "ascii")).decode("ascii"),
        }
        result = bucket.put_object(key, file, headers=headers, progress_callback=progress_callback)
        data = loads(result.resp.read())
        data["headers"] = result.headers
        return data

    def _oss_multipart_upload(
        self, 
        /, 
        bucket_name, 
        key, 
        file, 
        callback, 
        progress_callback=None, 
        *, 
        total_size=None, 
        part_size=None, 
    ):
        """帮助函数：上传文件到阿里云 OSS，分块上传，支持断点续传
        """
        uploadinfo = self.upload_url()
        token = self.request(uploadinfo["gettokenurl"], parse=loads)
        auth = oss2.Auth(token["AccessKeyId"], token["AccessKeySecret"])
        bucket = oss2.Bucket(auth, uploadinfo["endpoint"], bucket_name)
        if total_size is None:
            if hasattr(file, "fileno"):
                total_size = fstat(file).st_size
            else:
                total_size = stat(file).st_size
                file = open(file, "rb")
        part_size = oss2.determine_part_size(total_size, preferred_size=part_size or oss2.defaults.part_size)
        headers={
            "User-Agent": "aliyun-sdk-android/2.9.1", 
            "x-oss-security-token": token["SecurityToken"], 
        }
        upload_id = bucket.init_multipart_upload(key, headers=headers).upload_id
        parts = []
        offset = 0
        for part_number, (start, stop) in enumerate(cut_iter(total_size, step=part_size), 1):
            result = bucket.upload_part(
                key, 
                upload_id, 
                part_number, 
                oss2.SizedFileAdapter(file, stop-start), 
                progress_callback=progress_callback, 
                headers=headers, 
            )
            parts.append(oss2.models.PartInfo(part_number, result.etag, size=stop-start, part_crc=result.crc))
        headers["x-oss-callback"] = b64encode(bytes(ftoken["callback"]["callback"], "ascii")).decode("ascii")
        headers["x-oss-callback-var"] = b64encode(bytes(ftoken["callback"]["callback_var"], "ascii")).decode("ascii")
        result = bucket.complete_multipart_upload(key, upload_id, parts, headers=headers)
        data = loads(result.resp.read())
        data["headers"] = result.headers
        return data

    ########## Decompress API ##########

    def extract_push(self, /, payload: dict, **request_kwargs):
        """推送一个解压缩任务给服务器，完成后，就可以查看压缩包的文件列表了
        POST https://webapi.115.com/files/push_extract
        payload:
            - pick_code: str
            - secret: str = "" # 解压密码
        """
        api = "https://webapi.115.com/files/push_extract"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def extract_push_progress(self, /, payload: dict, **request_kwargs):
        """查询解压缩任务的进度
        GET https://webapi.115.com/files/push_extract
        payload:
            - pick_code: str
        """
        api = "https://webapi.115.com/files/push_extract"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def extract_info(self, /, payload: dict, **request_kwargs):
        """获取压缩文件的文件列表，推荐直接用封装函数 `extract_info`
        GET https://webapi.115.com/files/extract_info
        payload:
            - pick_code: str
            - file_name: str
            - paths: str
            - next_marker: str
            - page_count: int | str
        """
        api = "https://webapi.115.com/files/extract_info"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def extract_list(self, /, pick_code, path="", next_marker="", page_count=999, **request_kwargs):
        """获取压缩文件的文件列表，是对 `extract_info` 的封装，推荐使用
        """
        payload = {
            "pick_code": pick_code, 
            "file_name": path.strip("/"), 
            "paths": "文件", 
            "next_marker": next_marker, 
            "page_count": page_count, 
        }
        return self.extract_info(payload, **request_kwargs)

    def extract_add_file(self, /, payload: list[tuple[str, int | str]], **request_kwargs):
        """解压缩到某个文件夹，推荐直接用封装函数 `extract_file`
        POST https://webapi.115.com/files/add_extract_file
        payload:
            - pick_code: str
            - extract_file[]: str
            - extract_file[]: str
            - ...
            - to_pid: int | str = 0
            - paths: str = "文件"
        """
        api = "https://webapi.115.com/files/add_extract_file"
        return self.request(
            api, 
            "POST", 
            data=urlencode(payload), 
            parse=loads, 
            headers={"Content-Type": "application/x-www-form-urlencoded"}, 
            **request_kwargs, 
        )

    def extract_download_url_web(self, /, payload: dict, **request_kwargs):
        """获取压缩包中文件的下载链接
        GET https://webapi.115.com/files/extract_down_file
        payload:
            - pick_code: str
            - full_name: str
        """
        api = "https://webapi.115.com/files/extract_down_file"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    def extract_download_url(self, /, pick_code: str, full_name: str, **request_kwargs):
        """获取压缩包中文件的下载链接，此接口是对 `extract_download_url_web` 的封装
        """
        resp = self.extract_download_url_web({"pick_code": pick_code, "full_name": full_name}, **request_kwargs)
        if not resp["state"]:
            raise ValueError(resp)
        return resp["data"]["url"]

    def extract_file(self, /, pick_code, paths="", dir_="", to_pid=0, **request_kwargs):
        """解压缩到某个文件夹，是对 `extract_add_file` 的封装，推荐使用
        """
        dir_ = dir_.strip("/")
        dir2 = f"文件/{dir_}" if dir_ else "文件"
        data = [
            ("pick_code", pick_code), 
            ("paths", dir2), 
            ("to_pid", to_pid), 
        ]
        if not paths:
            resp = self.extract_info(pick_code, dir_)
            if not resp["state"]:
                return resp
            paths = (p["file_name"] if p["file_category"] else p["file_name"]+"/" for p in resp["data"]["list"])
            while (next_marker := resp["data"].get("next_marker")):
                resp = self.extract_info(pick_code, dir_, next_marker)
                paths.extend(p["file_name"] if p["file_category"] else p["file_name"]+"/" for p in resp["data"]["list"])
        if isinstance(paths, str):
            data.append(("extract_dir[]" if paths.endswith("/") else "extract_file[]", paths.strip("/")))
        else:
            data.extend(("extract_dir[]" if path.endswith("/") else "extract_file[]", path.strip("/")) for path in paths)
        return self.extract_add_file(data, **request_kwargs)

    def extract_progress(self, /, payload: dict, **request_kwargs):
        """获取 解压缩到文件夹 任务的进度
        GET https://webapi.115.com/files/add_extract_file
        payload:
            - extract_id: str
        """
        api = "https://webapi.115.com/files/add_extract_file"
        return self.request(api, params=payload, parse=loads, **request_kwargs)

    # TODO: 增加一个接口，下载压缩包中的文件

    ########## Offline Download API ##########

    # TODO: 增加一个接口，用于获取一个种子或磁力链接，里面的文件列表，这个文件并未被添加任务

    def offline_quota_package_info(self, /, **request_kwargs):
        """获取当前离线配额信息
        GET https://115.com/web/lixian/?ct=lixian&ac=get_quota_package_info
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=get_quota_package_info"
        return self.request(api, parse=loads, **request_kwargs)

    def offline_download_path(self, /, **request_kwargs):
        """获取当前默认的离线下载到的文件夹信息（可能有多个）
        GET https://webapi.115.com/offine/downpath
        """
        api = "https://webapi.115.com/offine/downpath"
        return self.request(api, parse=loads, **request_kwargs)

    def offline_upload_torrent_path(self, /, **request_kwargs):
        """获取当前的种子上传到的文件夹，当你添加种子任务后，这个种子会在此文件夹中保存
        GET https://115.com/?ct=lixian&ac=get_id&torrent=1
        """
        api = "https://115.com/?ct=lixian&ac=get_id&torrent=1"
        return self.request(api, parse=loads, **request_kwargs)

    def offline_getsign(self, /, **request_kwargs):
        """增删改查离线下载任务，需要携带签名 sign，具有一定的时效性，但不用每次都获取，失效了再用此接口获取就行了
        GET https://115.com/?ct=offline&ac=space
        """
        api = "https://115.com/?ct=offline&ac=space"
        return self.request(api, parse=loads, **request_kwargs)

    def offline_add_url(self, /, payload: dict, **request_kwargs):
        """添加一个离线任务
        POST https://115.com/web/lixian/?ct=lixian&ac=add_task_url
        payload:
            - uid: int | str
            - sign: str
            - time: int
            - savepath: str, 
            - wp_path_id: int | str 
            - url: str
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=add_task_url"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def offline_add_urls(self, /, payload: dict, **request_kwargs):
        """添加一组离线任务
        POST https://115.com/web/lixian/?ct=lixian&ac=add_task_urls
        payload:
            - uid: int | str
            - sign: str
            - time: int
            - savepath: str, 
            - wp_path_id: int | str 
            - url[0]: str
            - url[1]: str
            - ...
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=add_task_urls"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def offline_torrent_info(self, /, payload: dict, **request_kwargs):
        """查看离线任务的信息
        POST https://115.com/web/lixian/?ct=lixian&ac=torrent
        payload:
            - uid: int | str
            - sign: str
            - time: int
            - sha1: str
            - pickcode: str = ""
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=torrent"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def offline_add_torrent(self, /, payload: dict, **request_kwargs):
        """添加一个种子作为离线任务
        POST https://115.com/web/lixian/?ct=lixian&ac=add_task_bt
        payload:
            - uid: int | str
            - sign: str
            - time: int
            - savepath: str
            - info_hash: str
            - wanted: str
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=add_task_bt"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def offline_del(self, /, payload: dict, **request_kwargs):
        """删除一组离线任务（无论是否已经完成）
        POST https://115.com/web/lixian/?ct=lixian&ac=task_del
        payload:
            - uid: int | str
            - sign: str
            - time: int
            - hash[0]: str
            - hash[1]: str
            - ...
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=task_del"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    def offline_list(self, /, payload: dict, **request_kwargs):
        """获取当前的离线任务列表
        POST https://115.com/web/lixian/?ct=lixian&ac=task_lists
        payload:
            - page: int | str
            - uid: int | str
            - sign: str
            - time: int
        """
        api = "https://115.com/web/lixian/?ct=lixian&ac=task_lists"
        return self.request(api, "POST", data=payload, parse=loads, **request_kwargs)

    ########## Other Encapsulations ##########

    @cached_property
    def fs(self, /):
        return P115FileSystem(self)

    @cached_property
    def offline(self, /):
        return P115Offline(self)

    def get_share_fs(self, share_link: str, /) -> P115ShareFileSystem:
        return P115ShareFileSystem(self, share_link)

    def get_zip_fs(self, id: int, /) -> P115ZipFileSystem:
        return P115ZipFileSystem(self, id)

    def open(self, url, /, **request_kwargs) -> RequestsFileReader:
        return RequestsFileReader(url, urlopen=self.session.get)

    def read_bytes_range(self, url, /, bytes_range="0-", headers=None, **request_kwargs) -> bytes:
        if headers:
            headers = {**headers, "Accept-Encoding": "identity", "Range": f"bytes={bytes_range}"}
        else:
            headers = {"Accept-Encoding": "identity", "Range": f"bytes={bytes_range}"}
        with self.session.get(url, headers=headers, **request_kwargs) as resp:
            return resp.content

    def readrange(self, url, /, start=0, stop=None, headers=None, **request_kwargs) -> bytes:
        length = None
        if start < 0:
            length = int(cli.session.head(url).headers["Content-Length"])
            start += length
        if start < 0:
            start = 0
        if stop is None:
            bytes_range = f"{start}-"
        else:
            if stop < 0:
                if length is None:
                    length = int(cli.session.head(url).headers["Content-Length"])
                stop += length
            if stop <= 0 or start >= stop:
                return b""
            bytes_range = f"{start}-{stop}"
        return self.read_bytes_range(url, bytes_range, headers=headers, **request_kwargs)


class P115Path(Mapping, PathLike[str]):
    fs: P115FileSystem
    path: str

    def __init__(
        self, 
        /, 
        fs: P115FileSystem, 
        path: str | PathLike[str], 
        **attr, 
    ):
        super().__setattr__("__dict__", attr)
        attr.update(fs=fs, path=fs.abspath(path), attr_last_fetched=None)

    def __and__(self, path: str | PathLike[str], /) -> P115Path:
        return type(self)(self.fs, commonpath((self.path, self.fs.abspath(path))))

    def __call__(self, /):
        self.__dict__.update(self.fs.attr(self.path))
        self.__dict__["attr_last_fetched"] = datetime.now()
        return self

    def __contains__(self, key, /):
        return key in self.__dict__

    def __eq__(self, path, /) -> bool:
        return isinstance(path, P115Path) and self.fs.client == path.fs.client and self.path == path.path

    def __fspath__(self, /) -> str:
        return self.path

    def __getitem__(self, key, /):
        if key not in self.__dict__ and not self.attr_last_fetched:
            self()
        return self.__dict__[key]

    def __ge__(self, path, /):
        if not isinstance(path, P115Path) or self.fs.client != path.fs.client:
            return False
        return commonpath((self.path, path.path)) == path.path

    def __gt__(self, path, /):
        if not isinstance(path, P115Path) or self.fs.client != path.fs.client or self.path == path.path:
            return False
        return commonpath((self.path, path.path)) == path.path

    def __hash__(self, /):
        return hash(self.fs.client) ^ hash(self.path)

    def __iter__(self, /):
        return iter(self.__dict__)

    def __len__(self, /) -> int:
        return len(self.__dict__)

    def __le__(self, path, /):
        if not isinstance(path, P115Path) or self.fs.client != path.fs.client:
            return False
        return commonpath((self.path, path.path)) == self.path

    def __lt__(self, path, /):
        if not isinstance(path, P115Path) or self.fs.client != path.fs.client or self.path == path.path:
            return False
        return commonpath((self.path, path.path)) == self.path

    def __repr__(self, /) -> str:
        cls = type(self)
        module = cls.__module__
        name = cls.__qualname__
        if module != "__main__":
            name = module + "." + name
        return f"<{name}({', '.join(f'{k}={v!r}' for k, v in self.__dict__.items())})>"

    def __setattr__(self, attr, val, /):
        raise TypeError("can't set attribute")

    def __str__(self, /) -> str:
        return self.path

    def __truediv__(self, path: str | PathLike[str], /) -> P115Path:
        return type(self).joinpath(self, path)

    @cached_property
    def id(self, /):
        return self.fs.getid(self.path)

    def keys(self) -> KeysView:
        return self.__dict__.keys()

    def values(self) -> ValuesView:
        return self.__dict__.values()

    def items(self) -> ItemsView:
        return self.__dict__.items()

    @property
    def anchor(self, /) -> str:
        return "/"

    def as_uri(self, /) -> str:
        return self.url

    @property
    def attr(self, /) -> MappingProxyType:
        return MappingProxyType(self.__dict__)

    def exists(self, /) -> bool:
        return self.fs.exists(self.path)

    def glob(self, /, pattern: str, ignore_case: bool = False) -> Iterator[P115Path]:
        dirname = self.path if self.is_dir else self.parent.path
        return self.fs.glob(pattern, dirname, ignore_case=ignore_case)

    def isdir(self, /) -> bool:
        return self.fs.isdir(self.path)

    @property
    def is_dir(self, /):
        try:
            return self["isDirectory"]
        except FileNotFoundError:
            return False
        except KeyError:
            return False

    def isfile(self, /) -> bool:
        return self.fs.isfile(self.path)

    @property
    def is_file(self, /) -> bool:
        try:
            return not self["isDirectory"]
        except FileNotFoundError:
            return False
        except KeyError:
            return True

    def iterdir(
        self, 
        /, 
        refresh: Optional[bool] = None, 
        topdown: bool = True, 
        min_depth: int = 0, 
        max_depth: int = 1, 
        predicate: Optional[Callable[[P115Path], Optional[bool]]] = None, 
        onerror: Optional[bool] = None, 
    ) -> Iterator[P115Path]:
        return self.fs.iterdir(
            self.path, 
            refresh=refresh, 
            topdown=topdown, 
            min_depth=min_depth, 
            max_depth=max_depth, 
            predicate=predicate, 
            onerror=onerror, 
        )

    def joinpath(self, *args: str | PathLike[str]) -> P115Path:
        if not args:
            return self
        path = self.path
        path_new = normpath(joinpath(path, *args))
        if path == path_new:
            return self
        return type(self)(self.fs, path_new)

    def listdir(
        self, 
        /, 
        refresh: Optional[bool] = None, 
    ) -> list[str]:
        return self.fs.listdir(
            self.path, 
            refresh=refresh, 
        )

    def listdir_attr(
        self, 
        /, 
        refresh: Optional[bool] = None, 
    ) -> list[P115Path]:
        return self.fs.listdir_attr(
            self.path, 
            refresh=refresh, 
        )

    def match(self, /, path_pattern: str, ignore_case: bool = False) -> bool:
        pattern = joinpath("/", *(t[0] for t in posix_glob_translate_iter(path_pattern)))
        if ignore_case:
            pattern = "(?i:%s)" % pattern
        return re_compile(pattern).fullmatch(self.path) is not None

    def mkdir(self, /):
        self.fs.mkdir(self.path)

    def move(self, /, dst_path: str | PathLike[str]) -> P115Path:
        dst_path = self.fs.abspath(dst_path)
        dst_path = self.fs.move(self.path, dst_path)
        return type(self)(self.fs, dst_path)

    def open(
        self, 
        /, 
        mode: str = "r", 
        buffering: Optional[int] = None, 
        encoding: Optional[str] = None, 
        errors: Optional[str] = None, 
        newline: Optional[str] = None, 
    ):
        orig_mode = mode
        if "b" in mode:
            mode = mode.replace("b", "", 1)
            open_text_mode = False
        else:
            mode = mode.replace("t", "", 1)
            open_text_mode = True
        if mode not in ("r", "rt", "tr", "rb", "br"):
            raise OSError(errno.EINVAL, f"invalid (or unsupported) mode: {orig_mode!r}")
        if buffering is None:
            if open_text_mode:
                buffering = DEFAULT_BUFFER_SIZE
            else:
                buffering = 0
        if buffering == 0:
            if open_text_mode:
                raise OSError(errno.EINVAL, "can't have unbuffered text I/O")
            return CloudDriveFile(self, mode)
        line_buffering = False
        buffer_size: int
        if buffering < 0:
            buffer_size = DEFAULT_BUFFER_SIZE
        elif buffering == 1:
            if not open_text_mode:
                warn("line buffering (buffering=1) isn't supported in binary mode, "
                     "the default buffer size will be used", RuntimeWarning)
            buffer_size = DEFAULT_BUFFER_SIZE
            line_buffering = True
        else:
            buffer_size = buffering
        raw = CloudDriveFile(self, mode)
        buffer = BufferedReader(raw, buffer_size)
        if open_text_mode:
            return TextIOWrapper(
                buffer, 
                encoding=encoding, 
                errors=errors, 
                newline=newline, 
                line_buffering=line_buffering, 
            )
        else:
            return buffer

    @cached_property
    def parent(self, /) -> P115Path:
        path = self.path
        if path == "/":
            return self
        parent = dirname(path)
        if path == parent:
            return self
        return type(self)(self.fs, parent)

    @cached_property
    def parents(self, /) -> tuple[P115Path, ...]:
        path = self.path
        if path == "/":
            return ()
        parents: list[P115Path] = []
        cls, fs = type(self), self.fs
        parent = dirname(path)
        while path != parent:
            parents.append(cls(fs, parent))
        return tuple(parents)

    @cached_property
    def parts(self, /) -> tuple[str, ...]:
        return ("/", *self.path[1:].split("/"))

    def read_bytes(self, /):
        return self.open("rb").read()

    # TODO def read_range

    def read_text(
        self, 
        /, 
        encoding: Optional[str] = None, 
        errors: Optional[str] = None, 
    ):
        return self.open(encoding=encoding, errors=errors).read()

    def remove(self, /, recursive: bool = False):
        self.fs.remove(self.path, recursive=recursive)

    def rename(
        self, 
        /, 
        dst_path: str | PathLike[str], 
    ) -> P115Path:
        dst_path = self.fs.abspath(dst_path)
        self.fs.rename(self.path, dst_path)
        return type(self)(self.fs, dst_path)

    def replace(
        self, 
        /, 
        dst_path: str | PathLike[str], 
    ) -> P115Path:
        dst_path = self.fs.abspath(dst_path)
        self.fs.replace(self.path, dst_path)
        return type(self)(self.fs, dst_path)

    def rglob(self, /, pattern: str, ignore_case: bool = False) -> Iterator[P115Path]:
        dirname = self.path if self.is_dir else self.parent.path
        return self.fs.rglob(pattern, dirname, ignore_case=ignore_case)

    def rmdir(self, /):
        self.fs.rmdir(self.path)

    @cached_property
    def root(self, /) -> P115Path:
        parents = self.parents
        if not parents:
            return self
        elif len(parents) == 1:
            return self
        return parents[-2]

    def samefile(self, path: str | PathLike[str], /) -> bool:
        if isinstance(path, P115Path):
            return self == path
        return self.path == self.fs.abspath(path)

    def stat(self, /):
        return self.fs.stat(self.path)

    @cached_property
    def stem(self, /) -> str:
        return splitext(basename(self.path))[0]

    @cached_property
    def suffix(self, /) -> str:
        return splitext(basename(self.path))[1]

    @cached_property
    def suffixes(self, /) -> tuple[str, ...]:
        return tuple("." + part for part in basename(self.path).split(".")[1:])

    def touch(self, /):
        self.fs.touch(self.path)

    unlink = remove

    @property
    def url(self, /) -> str:
        return self.fs.get_url(self.id)

    def with_name(self, name: str, /) -> P115Path:
        return self.parent.joinpath(name)

    def with_stem(self, stem: str, /) -> P115Path:
        return self.parent.joinpath(stem + self.suffix)

    def with_suffix(self, suffix: str, /) -> P115Path:
        return self.parent.joinpath(self.stem + suffix)

    def write_bytes(self, data: bytes | bytearray, /):
        bio = BytesIO(data)
        return self.fs.upload(bio, self.path, overwrite_or_ignore=True)

    def write_text(
        self, 
        text: str, 
        /, 
        encoding: Optional[str] = None, 
        errors: Optional[str] = None, 
        newline: Optional[str] = None, 
    ):
        bio = BytesIO()
        if text:
            tio = TextIOWrapper(bio, encoding=encoding, errors=errors, newline=newline)
            tio.write(text)
            bio.seek(0)
        return self.fs.upload(bio, self.path, overwrite_or_ignore=True)

# TODO 增加几种文件系统：普通（增删改查）、压缩包（查，解压(extract)）、分享文件夹（查，转存(transfer)）
# TODO 如果压缩包尚未解压，则使用 zipfile 之类的模块，去模拟文件系统
# TODO 实现清空已完成，清空所有失败任务，清空所有未完成，具体参考app（抓包）
# TODO 以后会支持传入作为缓存的 MutableMapping
# TODO 如果以后有缓存的话，getcwd 会获取最新的名字

class P115FileSystem:
    client: P115Client
    cid: int
    path: str

    def __init__(self, client, /):
        self.__dict__.update(
            client = client, 
            cid = 0, 
            path = "/", 
        )

    def __iter__(self, /):
        return self.iterdir(max_depth=-1)

    def __itruediv__(self, /, id_or_path: ID_OR_PATH_TYPE):
        self.chdir(path)
        return self

    def __repr__(self, /) -> str:
        cls = type(self)
        module = cls.__module__
        name = cls.__qualname__
        if module != "__main__":
            name = module + "." + name
        return f"<{name}(client={self.client!r}, cid={self.cid!r}, path={self.path!r}) at {hex(id(self))}>"

    def __setattr__(self, attr, val, /):
        raise TypeError("can't set attribute")

    def abspath(self, path: str = "", /) -> str:
        if path in ("", "."):
            return self.path
        return normpath(joinpath(self.path, path))

    def _attr(self, id: int, /) -> dict:
        if id == 0:
            raise PermissionError(errno.EPERM, "the attributes of the root are not readable")
        resp = self.client.fs_info({"file_id": id})
        if not resp["state"]:
            raise FileNotFoundError(errno.ENOENT, f"no such cid/file_id: {id!r}")
        return normalize_info(resp["data"][0])

    def _attr_path(self, path: str, pid: Optional[int] = None, /) -> dict:
        if path:
            path = normpath(path)
            if path.startswith("/"):
                pid = 0
                path = path.lstrip("/")
        if pid is None:
            pid = self.cid
        if not path:
            return self._attr(pid)
        if pid:
            attr = self._attr(pid)
        else:
            attr = {"is_dir": True}
        for name in path.split("/"):
            if name == ".":
                continue
            elif name == "..":
                pid = attr["parent_id"]
                if pid == 0:
                    attr = {"is_dir": True}
                else:
                    attr = self._attr(pid)
            if not attr["is_dir"]:
                raise NotADirectoryError(errno.ENOTDIR, f"`pid` does not point to a directory: {pid!r}")
            for attr in self._iterdir(pid):
                if attr["name"].replace("/", "|") == name:
                    pid = attr["id"]
                    break
            else:
                raise FileNotFoundError(errno.ENOENT, f"no such file {path!r} in the directory {pid!r}")
        if pid == 0:
            raise PermissionError(errno.EPERM, "the attributes of the root are not readable")
        return attr

    def _attr_patht(self, patht: Sequence[str], pid: Optional[int] = None, /) -> dict:
        if "" in patht:
            patht = tuple(p for p in patht if p)
        if pid is None:
            pid = self.cid
        if not patht:
            return self._attr(pid)
        if pid:
            attr = self._attr(pid)
        else:
            attr = {"is_dir": True}
        for name in patht:
            if not attr["is_dir"]:
                raise NotADirectoryError(errno.ENOTDIR, f"`pid` does not point to a directory: {pid!r}")
            for attr in self._iterdir(pid):
                if attr["name"] == name:
                    pid = attr["id"]
                    break
            else:
                raise FileNotFoundError(errno.ENOENT, f"no such file {name!r} in the directory {pid!r}")
        if attr is None:
            return self._attr(pid)
        return attr

    def attr(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> dict:
        if isinstance(id_or_path, int):
            return self._attr(id_or_path)
        elif isinstance(id_or_path, str):
            return self._attr_path(id_or_path, pid)
        else:
            return self._attr_patht(id_or_path, pid)

    # TODO 各种 batch_* 方法

    def chdir(self, /, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None):
        if id_or_path in (0, "/"):
            self.__dict__.update(cid=0, path="/")
            return 0
        elif id_or_path == "":
            return self.cid
        attr = self.attr(id_or_path, pid)
        if self.cid == attr["id"]:
            return self.cid
        elif attr["is_dir"]:
            if isinstance(id_or_path, int):
                path = self.getpath(id_or_path)
            elif isinstance(id_or_path, str):
                if id_or_path.startswith("/"):
                    path = normpath(id_or_path)
                else:
                    path = normpath(joinpath(self.getpath(pid), id_or_path))
            else:
                path = joinpath(self.getpath(pid), *(p.replace("/", "|") for p in id_or_path if p))
            self.__dict__.update(cid=attr["id"], path=path)
            return attr["id"]
        else:
            raise NotADirectoryError(errno.ENOTDIR, f"{id_or_path!r} (in {pid!r}) is not a directory")

    # TODO def copy
    # TODO def copytree
    # TODO def download
    # TODO def download_tree

    def exists(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> bool:
        try:
            self.attr(id_or_path, pid)
            return True
        except PermissionError:
            return True
        except FileNotFoundError:
            return False

    def getcwd(self, /) -> str:
        return self.path

    def get_url(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> bool:
        attr = self.attr(id_or_path, pid)
        if attr["is_dir"]:
            raise IsADirectoryError(errno.EISDIR, f"{id_or_path!r} (in {pid!r}) is a directory")
        return self.client.download_url(attr["pick_code"])

    def getid(self, path: str | Sequence[str], pid: Optional[int] = None, /) -> int:
        try:
            if isinstance(pid, str):
                return self._attr_path(path, pid)["id"]
            else:
                return self._attr_patht(path, pid)["id"]
        except PermissionError:
            return 0

    def getpath(self, id: Optional[int] = None, /) -> str:
        if id is None:
            return self.path
        elif id == 0:
            return "/"
        ls = []
        add = ls.append
        while id:
            attr = self._attr(id)
            add(attr["name"].replace("/", "|"))
            id = attr["parent_id"]
        return "/" + "/".join(reversed(ls))

    # TODO def glob

    def isdir(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> bool:
        try:
            return self.attr(id_or_path, pid)["is_dir"]
        except PermissionError:
            return True
        except FileNotFoundError:
            return False

    def isfile(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> bool:
        try:
            return not self.attr(id_or_path, pid)["is_dir"]
        except PermissionError:
            return False
        except FileNotFoundError:
            return False

    # TODO def is_empty

    def _iterdir(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /, offset_size: int = 100):
        assert offset_size > 0
        attr = self.attr(id_or_path, pid)
        if not attr["is_dir"]:
            raise NotADirectoryError(errno.ENOTDIR, f"{id_or_path!r} (in {pid!r}) is not a directory")
        fs_files = self.client.fs_files
        payload = {
            "cid": attr["id"], 
            "offset": 0, 
            "limit": offset_size, 
            "show_dir": 1, 
        }
        resp = fs_files(payload)
        yield from map(normalize_info, resp["data"])
        for offset in range(offset_size, resp["count"], offset_size):
            payload["offset"] = offset
            resp = fs_files(payload)
            yield from map(normalize_info, resp["data"])

    # TODO def iterdir

    def listdir(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> list[str]:
        return [attr["name"] for attr in self._iterdir(id_or_path, pid, 1 << 10)]

    # TODO
    def listdir_attr(self, id_or_path: ID_OR_PATH_TYPE, pid: Optional[int] = None, /) -> list[dict]:
        return list(self._iterdir(id_or_path, pid, 1 << 10))

    # TODO def makedirs
    # TODO def mkdir
    # TODO def move
    # TODO def open
    # TODO def remove
    # TODO def removedirs
    # TODO def rename
    # TODO def renames
    # TODO def replace
    # TODO def rglob
    # TODO def rmdir
    # TODO def rmtree

    def scandir(
        self, 
        /, 
        id_or_path: ID_OR_PATH_TYPE, 
        pid: Optional[int] = None, 
    ):
        raise UnsupportedOperation(errno.ENOSYS, 
            "`scandir()` is currently not supported, use `iterdir()` instead."
        )

    # TODO def search

    def stat(
        self, 
        /, 
        id_or_path: ID_OR_PATH_TYPE, 
        pid: Optional[int] = None, 
    ):
        raise UnsupportedOperation(errno.ENOSYS, 
            "`stat()` is currently not supported, use `attr()` instead."
        )

    # TODO def touch
    # TODO def upload
    # TODO def upload_tree
    # TODO def unlink
    # TODO def walk
    # TODO def walk_attr

    cd  = chdir
    pwd = getcwd
    ls  = listdir
    ll  = listdir_attr
    rm  = remove


class P115SharePath(Mapping, PathLike[str]):
    fs: P115ShareFileSystem
    path: str


# TODO 比照 P115FileSystem 但没有增删改方法
class P115ShareFileSystem:

    def __init__(self, client: P115Client, /, share_link: str, path: str = "/"):
        self._client = client
        m = CRE_SHARE_LINK.search(share_link)
        if m is None:
            raise ValueError("not a valid 115 share link")
        self._share_link = share_link
        self._params = {"share_code": m["share_code"], "receive_code": m["receive_code"] or ""}
        self._path_to_id = {"/": 0}
        self._id_to_path = {0: "/"}
        self._id_to_attr = {}
        self._id_to_url = {}
        self._pid_to_attrs = {}
        self._full_loaded = False
        self._path = "/" + normpath(path).rstrip("/")

    def __repr__(self, /) -> str:
        cls = type(self)
        module = cls.__module__
        name = cls.__qualname__
        if module != "__main__":
            name = module + "." + name
        return f"{name}(client={self._client!r}, share_link={self._share_link!r}, path={self._path!r})"

    def _attr(self, id_or_path: int | str, /) -> dict:
        if isinstance(id_or_path, str):
            return self._attr_path(id_or_path)
        else:
            return self._attr_id(id_or_path)

    def _attr_id(self, id: int, /) -> dict:
        if id == 0:
            raise PermissionError(errno.EPERM, "the attributes of the root are not readable")
        if id in self._id_to_attr:
            return self._id_to_attr[id]
        if self._full_loaded:
            raise FileNotFoundError(errno.ENOENT, f"no such cid/file_id: {id!r}")
        dq = deque((0,))
        while dq:
            pid = dq.popleft()
            for attr in self._listdir(pid):
                if attr["id"] == id:
                    return attr
                if attr["is_dir"]:
                    dq.append(attr["id"])
        self._full_loaded = True
        raise FileNotFoundError(errno.ENOENT, f"no such cid/file_id: {id!r}")

    def _attr_path(self, path: str, /) -> dict:
        path = self.abspath(path)
        if path == "/":
            raise PermissionError(errno.EPERM, "the attributes of the root are not readable")
        if path in self._path_to_id:
            id = self._path_to_id[path]
            return self._id_to_attr[id]
        if self._full_loaded:
            raise FileNotFoundError(errno.ENOENT, f"no such path: {path!r}")
        ppath = dirname(path)
        ls_ppath = [ppath]
        while ppath not in self._path_to_id:
            ppath = dirname(ppath)
            ls_ppath.append(ppath)
        try:
            for ppath in reversed(ls_ppath):
                pid = self._path_to_id[ppath]
                attrs = self._listdir(pid)
                if not attrs or attrs[0]["id"] in self._id_to_path:
                    raise FileNotFoundError(errno.ENOENT, f"no such path: {path!r}")
                for attr in attrs:
                    psid = attr["id"]
                    pspath = joinpath(ppath, attr["name"])
                    self._path_to_id[pspath] = psid
                    self._id_to_path[psid] = pspath
            id = self._path_to_id[path]
            return self._id_to_attr[id]
        except KeyError:
            raise FileNotFoundError(errno.ENOENT, f"no such path: {path!r}")

    def _listdir(self, id_or_path: int | str = "", /) -> list[dict]:
        if isinstance(id_or_path, str):
            if id_or_path == "":
                id = self._path_to_id[self._path]
            elif self.abspath(id_or_path) == "/":
                id = 0
            else:
                id = self._attr_path(id_or_path)["id"]
        else:
            id = id_or_path
        if id in self._pid_to_attrs:
            return self._pid_to_attrs[id]
        if self._full_loaded:
            raise FileNotFoundError(errno.ENOENT, f"no such cid/file_id: {id!r}")
        params = {**self._params, "cid": id, "offset": 0, "limit": 100}
        data = check_get(self.client.share_snap(params))
        ls = list(map(normalize_info, data["list"]))
        count = data["count"]
        if count > 100:
            for offset in range(100, count, 100):
                params["offset"] = offset
                data = check_get(self.client.share_snap(params))
                ls.extend(map(normalize_info, data["list"]))
        self._id_to_attr.update((attr["id"], attr) for attr in ls)
        self._pid_to_attrs[id] = ls
        return ls

    def abspath(self, path: str, /) -> str:
        return normpath(joinpath(self._path, path))

    def attr(self, id_or_path: int | str) -> dict:
        return deepcopy(self._attr(id_or_path))

    def chdir(self, path: str = "/", /):
        if path == "":
            return
        path = self.abspath(path)
        if path == "/":
            self._path = "/"
        else:
            if self._attr_path(path)["is_dir"]:
                self._path = path

    @property
    def client(self, /) -> P115Client:
        return self._client

    def exists(self, id_or_path: int | str = 0, /):
        try:
            self._attr(id_or_path)
            return True
        except FileNotFoundError:
            return False
        except PermissionError:
            return True

    def getcwd(self, /) -> str:
        return self._path

    def get_download_url(self, id_or_path: int | str = 0, /) -> str:
        if isinstance(id_or_path, str):
            id = self._attr_path(id_or_path)["id"]
        else:
            id = id_or_path
        if id in self._id_to_url and time() + 60 * 30 < self._id_to_url[id]["expire"]:
            return self._id_to_url[id]["url"]
        payload = {**self._params, "file_id": id}
        url = self.client.share_download_url(payload)
        self._id_to_url[id] = {"url": url, "expire": int(parse_qsl(urlparse(url).query)[0][1])}
        return url

    def isdir(self, id_or_path: int | str = 0, /) -> bool:
        try:
            return self._attr(id_or_path)["is_dir"]
        except FileNotFoundError:
            return False
        except PermissionError:
            return True

    def isfile(self, id_or_path: int | str = 0, /) -> bool:
        try:
            return not self._attr(id_or_path)["is_dir"]
        except FileNotFoundError:
            return False
        except PermissionError:
            return False

    def iterdir(
        self, 
        id_or_path: int | str = "", 
        /, 
        topdown: bool = True, 
        max_depth: int = 1, 
        predicate: Optional[Callable[[str, dict], Optional[bool]]] = None, 
        onerror: Optional[bool] = None, 
    ) -> Iterator[tuple[str, dict]]:
        if not max_depth:
            return
        try:
            ls = self._listdir(id_or_path)
        except OSError as e:
            if callable(onerror):
                onerror(e)
            elif onerror:
                raise
            return
        if isinstance(id_or_path, str):
            top = self.abspath(id_or_path)
        else:
            top = self._id_to_path[id_or_path]
        if max_depth > 0:
            max_depth -= 1
        for attr in ls:
            path = joinpath(top, attr["name"])
            yield_me = True
            if predicate:
                pred = predicate(path, attr)
                if pred is None:
                    continue
                yield_me = pred
            if topdown and yield_me:
                yield path, attr
            if attr["is_dir"]:
                yield from self.iterdir(
                    path, 
                    topdown=topdown, 
                    max_depth=max_depth, 
                    predicate=predicate, 
                    onerror=onerror, 
                )
            if not topdown and yield_me:
                yield path, attr

    def listdir(self, id_or_path: int | str = 0, /) -> list[str]:
        return [attr["name"] for attr in self._listdir(id_or_path)]

    def listdir_attr(self, id_or_path: int | str = 0, /) -> list[dict]:
        return deepcopy(self._listdir(id_or_path))

    path = property(getcwd, chdir)

    def receive(self, ids: int | str | Iterable[int | str], cid=0):
        if isinstance(ids, (int, str)):
            file_id = str(ids)
        else:
            file_id = ",".join(map(str, ids))
            if not file_id:
                raise ValueError("no id (to file) to transfer")
        payload = {**self._params, "file_id": file_id, "cid": cid}
        check_get(self.client.share_receive(payload))

    @property
    def shareinfo(self, /) -> dict:
        return check_get(self.client.share_snap({**self._params, "limit": 1}))["shareinfo"]

    @property
    def share_link(self, /) -> str:
        return self._share_link

    def walk(
        self, 
        id_or_path: int | str = "", 
        /, 
        topdown: bool = True, 
        max_depth: int = -1, 
        onerror: None | bool | Callable = None, 
    ) -> Iterator[tuple[str, list[str], list[str]]]:
        if not max_depth:
            return
        try:
            ls = self._listdir(id_or_path)
        except OSError as e:
            if callable(onerror):
                onerror(e)
            elif onerror:
                raise
            return
        if isinstance(id_or_path, str):
            top = self.abspath(id_or_path)
        else:
            top = self._id_to_path[id_or_path]
        if not ls:
            yield top, [], []
            return
        dirs: list[str] = []
        files: list[str] = []
        for attr in ls:
            if attr["is_dir"]:
                dirs.append(attr["name"])
            else:
                files.append(attr["name"])
        if topdown:
            yield top, dirs, files
        if max_depth > 0:
            max_depth -= 1
        for dir_ in dirs:
            yield from self.walk(
                joinpath(top, dir_), 
                topdown=topdown, 
                max_depth=max_depth, 
                onerror=onerror, 
            )
        if not topdown:
            yield top, dirs, files

    def walk_attr(
        self, 
        id_or_path: int | str = "", 
        /, 
        topdown: bool = True, 
        max_depth: int = -1, 
        onerror: None | bool | Callable = None, 
    ) -> Iterator[tuple[str, list[dict], list[dict]]]:
        if not max_depth:
            return
        try:
            ls = self._listdir(id_or_path)
        except OSError as e:
            if callable(onerror):
                onerror(e)
            elif onerror:
                raise
            return
        if isinstance(id_or_path, str):
            top = self.abspath(id_or_path)
        else:
            top = self._id_to_path[id_or_path]
        if not ls:
            yield top, [], []
            return
        dirs: list[str] = []
        files: list[str] = []
        for attr in ls:
            if attr["is_dir"]:
                dirs.append(attr)
            else:
                files.append(attr)
        if topdown:
            yield top, dirs, files
        if max_depth > 0:
            max_depth -= 1
        for dir_ in dirs:
            yield from self.walk_attr(
                joinpath(top, dir_["name"]), 
                topdown=topdown, 
                max_depth=max_depth, 
                onerror=onerror, 
            )
        if not topdown:
            yield top, dirs, files

    cd = chdir
    pwd = getcwd
    ls = listdir
    ll = listdir_attr


class P115ZipPath(Mapping, PathLike[str]):
    fs: P115ZipFileSystem
    path: str


class P115ZipFileSystem:

    def __init__(self, client: P115Client, /, id: int):
        ...


# TODO 清除已完成
# TODO 用户、密码登陆，直接用app端接口，不需要验证码
class P115Offline:

    def __init__(self, client, /):
        self.client = client
        self.uid = client.userid
        self.refresh_sign()

    def refresh_sign(self, /):
        self.sign = self.client.offline_getsign()["sign"]

    def add_url(self, /, url, pid="", savepath=""):
        payload = {
            "url": url,
            "savepath": savepath, 
            "wp_path_id": pid, 
            "uid": self.uid, 
            "sign": self.sign, 
            "time": int(time()), 
        }
        return self.client.offline_add_url(payload)

    def add_urls(self, /, urls, pid="", savepath=""):
        payload = {
            "savepath": savepath, 
            "wp_path_id": pid, 
            "uid": self.uid, 
            "sign": self.sign, 
            "time": int(time()), 
        }
        payload.update((f"url[{i}]", url) for i, url in enumerate(urls))
        return self.client.offline_add_urls(payload)

    def task_list(self, page=0):
        if page > 0:
            payload = {
                "page": page, 
                "uid": self.uid, 
                "sign": self.sign, 
                "time": int(time()), 
            }
            return self.client.offline_list(payload)
        page = 1
        payload = {
            "page": page, 
            "uid": self.uid, 
            "sign": self.sign, 
            "time": int(time()), 
        }
        resp = self.client.offline_list(payload)
        resp["page"] = 0
        ls = resp['tasks']
        if not ls:
            return resp
        for page in range(2, resp["page_count"]+1):
            payload["page"] = page
            sub_resp = self.client.offline_list(payload)
            ls.extend(sub_resp["tasks"])
        return resp

    def torrent_info(self, /, sha1_or_fid):
        payload = {
            "uid": self.uid, 
            "sign": self.sign, 
            "time": int(time()), 
        }
        if isinstance(sha1_or_fid, int):
            resp = self.client.fs_file(sha1_or_fid)
            data = resp["data"][0]
            payload["pickcode"] = data["pick_code"]
            payload["sha1"] = data["sha1"]
        else:
            payload["sha1"] = sha1_or_fid
        return self.client.offline_torrent_info(payload)

    def add_torrent(self, /, sha1_or_fid, savepath="", filter_func=None):
        resp = self.torrent_info(sha1_or_fid)
        if not resp["state"]:
            raise RuntimeError(resp)
        filelist = filter(filter_func, resp["torrent_filelist_web"])
        payload = {
            "wanted": ",".join(str(info["wanted"]) for info in filelist), 
            "info_hash": resp["info_hash"], 
            "savepath": savepath or resp["torrent_name"], 
            "uid": self.uid, 
            "sign": self.sign, 
            "time": int(time()), 
        }
        return self.client.offline_add_torrent(payload)

    def del_tasks(self, /, task_hashes):
        payload = {
            "uid": self.uid, 
            "sign": self.sign, 
            "time": int(time()), 
        }
        payload.update((f"hash[{i}]", h) for i, h in enumerate(task_hashes))
        return self.client.offline_del(payload)


# TODO: 对回收站的封装
# TODO: 能及时处理文件已不存在
# TODO: 115中多个文件可以在同一目录下同名，如何处理？
# TODO: 是否要支持 cd2 的改名策略，也就是遇到 **.，则自动忽略后面的所有部分，改名时，改成 新名字 + **. + 原始扩展名
# TODO: 批量改名工具：如果后缀名一样，则直接改名，如果修改了后缀名，那就尝试用秒传，重新上传，上传如果失败，因为名字问题，则尝试用uuid名字，上传成功后，再进行改名，如果成功，删除原来的文件，不成功，则删掉上传的文件（如果上传了的话）
#       - file 参数支持接受一个 callable：由于上传时，给出 sha1 本身可能就能成功，为了速度，我认为，一开始就不需要打开一个文件
#       - 增加 readrange: 由于文件可以一开始就 seek 到目标位置，因此一开始就可以指定对应的位置，因此可以添加一个方法，叫 readrange，直接读取一定范围的 http 数据

# TODO: File 对象要可以获取 url，而且尽量利用 client 上的方法，ShareFile 也要有相关方法（例如转存）
# TODO: 支持异步io，用 aiohttp


"""上传文件的存取层。

安全上有两条硬要求（都不是多余的）：

1. **file_id 必须先过格式校验再拼路径。** 否则 ``file_id = "../../../etc/passwd"``
   就能读到任意文件。校验必须是「白名单格式」（仅 12 位十六进制），
   而不是「过滤掉 ..」—— 黑名单永远漏。
2. **resolve() 之后再做一次越界确认。** 即使格式合法，也要确认最终路径确实
   落在存储目录内（防符号链接）。这两道一起才叫「防住了」。

``persist=False`` 时只放内存，供测试使用 —— 这样测试跑完不会在仓库里留下残留文件。
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: 合法的 file_id：12 位十六进制
ID_PATTERN = re.compile(r"^[0-9a-f]{12}$")

#: 单个文件的元信息文件名后缀
META_SUFFIX = ".json"
BLOB_SUFFIX = ".bin"


class FileNotFoundInStore(KeyError):
    """文件不存在（或 id 非法）。"""


def new_id() -> str:
    return secrets.token_hex(6)  # 6 字节 → 12 位十六进制


def is_valid_id(file_id: str) -> bool:
    """id 合法性**前置**校验：先看格式，再谈路径。"""
    return bool(file_id) and ID_PATTERN.match(file_id) is not None


@dataclass
class StoredFile:
    file_id: str
    name: str
    size: int = 0
    kind: str = ""
    detail: str = ""
    created_at: float = 0.0
    summary: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class FileStore:
    """上传文件的存取。内存 + 磁盘双份，保证测试与生产都能跑。"""

    def __init__(self, root: Path, *, persist: bool = True) -> None:
        self.root = Path(root)
        self.persist = persist
        self._memory: dict[str, bytes] = {}
        self._meta: dict[str, StoredFile] = {}
        if persist:
            self.root.mkdir(parents=True, exist_ok=True)

    # -- 路径 --------------------------------------------------------------

    def _blob_path(self, file_id: str) -> Path:
        if not is_valid_id(file_id):
            raise FileNotFoundInStore(file_id)
        candidate = (self.root / f"{file_id}{BLOB_SUFFIX}").resolve()
        root = self.root.resolve()
        # 第二道：确认没有越界（resolve 会展开符号链接与 ..）
        if root not in candidate.parents:
            logger.warning("拒绝越界访问：%s", candidate)
            raise FileNotFoundInStore(file_id)
        return candidate

    def _meta_path(self, file_id: str) -> Path:
        return self._blob_path(file_id).with_suffix(META_SUFFIX)

    # -- 写 ----------------------------------------------------------------

    def save(self, name: str, data: bytes, *, kind: str = "", detail: str = "", summary: dict | None = None) -> StoredFile:
        """保存一份上传文件，返回元信息。"""
        file_id = new_id()
        record = StoredFile(
            file_id=file_id,
            name=name,
            size=len(data),
            kind=kind,
            detail=detail,
            created_at=time.time(),
            summary=summary or {},
        )
        self._memory[file_id] = data
        self._meta[file_id] = record
        if self.persist:
            blob = self._blob_path(file_id)
            blob.write_bytes(data)
            blob.with_suffix(META_SUFFIX).write_text(
                json.dumps(record.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return record

    # -- 读 ----------------------------------------------------------------

    def get_bytes(self, file_id: str) -> bytes:
        if file_id in self._memory:
            return self._memory[file_id]
        path = self._blob_path(file_id)
        if not path.exists():
            raise FileNotFoundInStore(file_id)
        return path.read_bytes()

    def get_meta(self, file_id: str) -> StoredFile:
        if file_id in self._meta:
            return self._meta[file_id]
        path = self._meta_path(file_id)
        if not path.exists():
            raise FileNotFoundInStore(file_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # 元信息损坏只影响这一条记录，不该让整个列表接口挂掉
            logger.warning("元信息损坏，已跳过 %s：%s", file_id, exc)
            raise FileNotFoundInStore(file_id) from exc
        return StoredFile(**payload)

    def list_files(self) -> list[StoredFile]:
        """列出全部文件，按上传时间倒序。

        磁盘上的元信息损坏时**只跳过这一条**，不影响其余记录 ——
        一个坏文件不应该让整个列表页打不开。
        """
        out: dict[str, StoredFile] = dict(self._meta)
        if self.persist and self.root.exists():
            for meta_file in self.root.glob(f"*{META_SUFFIX}"):
                file_id = meta_file.stem
                if file_id in out or not is_valid_id(file_id):
                    continue
                try:
                    payload = json.loads(meta_file.read_text(encoding="utf-8"))
                    out[file_id] = StoredFile(**payload)
                except Exception as exc:
                    logger.warning("跳过损坏的元信息 %s：%s", meta_file.name, exc)
        return sorted(out.values(), key=lambda r: r.created_at, reverse=True)

    def delete(self, file_id: str) -> bool:
        existed = file_id in self._memory or file_id in self._meta
        self._memory.pop(file_id, None)
        self._meta.pop(file_id, None)
        if self.persist:
            for path in (self._blob_path(file_id), self._meta_path(file_id)):
                if path.exists():
                    existed = True
                    try:
                        path.unlink()
                    except OSError as exc:
                        logger.warning("删除 %s 失败：%s", path, exc)
        return existed

    def count(self) -> int:
        return len(self.list_files())


__all__ = [
    "ID_PATTERN",
    "FileNotFoundInStore",
    "StoredFile",
    "FileStore",
    "new_id",
    "is_valid_id",
]

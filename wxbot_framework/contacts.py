"""微信 4.1.15.13 联系人缓存的只读名字快照；调用方必须先核对核心 DLL 指纹。"""
from __future__ import annotations

import struct

from .account import APP_GLOBAL_RVA, _pointer, read_current_account, validate_account

CACHE_PROVIDER_GETTER_RVA = 0x36C7B0
CACHE_PROVIDER_VTABLE_RVA = 0x8F9BF98
CONTACT_CACHE_VTABLE_RVA = 0x8FA2538
CHATROOM_CACHE_VTABLE_RVA = 0x8F9C228
INVISIBLE_CHARS = "\u200b\u200c\u200d\u2060\ufeff\u3164\u115f\u1160\uffa0\u2800\u00a0\u2005"


def _clean_contact_name(name):
    if not isinstance(name, str) or len(name) > 64 or any(ord(ch) < 32 for ch in name):
        return None
    cleaned = name.strip(" \t\r\n" + INVISIBLE_CHARS)
    if not cleaned or all(ch in INVISIBLE_CHARS or ch.isspace() for ch in cleaned):
        return None
    return cleaned


def _fnv1a64(text):
    hash_value = 0xCBF29CE484222325
    for byte in text.encode("ascii"):
        hash_value = ((hash_value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return hash_value


def read_contact_name(read_memory, core_base, wxid, *, expected_account=None):
    """按实际成员 ID 读取微信名。缺失、切换、重建缓存时返回 None，不猜名字。

    仅遍历已核对的缓存对象，不调用微信函数、不扫描整个进程、不读取群昵称或备注。
    """
    validate_account(wxid)
    account = read_current_account(read_memory, core_base)
    if not account or (expected_account is not None and account != expected_account):
        return None
    try:
        def read(address, length):
            value = read_memory(_pointer(address), length)
            if len(value) != length:
                raise ValueError("联系人名字读取不完整")
            return value

        def ptr(address):
            return struct.unpack("<Q", read(address, 8))[0]

        def string(address, limit=256):
            header = read(address, 32)
            length, capacity = struct.unpack_from("<QQ", header, 16)
            if not 1 <= length <= limit or capacity < length or not (capacity == 15 or 16 <= capacity <= 1048576):
                raise ValueError("联系人字符串布局不匹配")
            buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
            body = read(buffer, length)
            if read(address, 32) != header or read(buffer, length) != body:
                raise ValueError("联系人字符串发生变化")
            return body.decode("utf-8")

        app = ptr(core_base + APP_GLOBAL_RVA)
        context = ptr(app + 0x68)
        table = ptr(context)
        if ptr(table + 0x38) != core_base + CACHE_PROVIDER_GETTER_RVA:
            return None
        if read(core_base + CACHE_PROVIDER_GETTER_RVA + 28, 8) != bytes.fromhex("48 83 b9 60 04 00 00 00"):
            return None
        provider = _pointer(ptr(context + 0x460))
        if ptr(provider) != core_base + CACHE_PROVIDER_VTABLE_RVA:
            return None
        registry = read(provider + 0x90, 16)
        head, size = struct.unpack("<QQ", registry)
        if not 1 <= size <= 512:
            return None
        node = ptr(head)
        visited, cache = set(), None
        for _ in range(size):
            if node == head or node in visited:
                break
            visited.add(node)
            value = ptr(node + 0x30)
            if value and ptr(value) == core_base + CONTACT_CACHE_VTABLE_RVA:
                cache = value
                break
            node = ptr(node)
        if not cache or read(provider + 0x90, 16) != registry:
            return None

        # MSVC unordered_map：哨兵、元素数、桶首尾和桶掩码都要在读取前后保持一致。
        layout = read(cache + 0xC8, 0x38)
        sentinel, count = struct.unpack_from("<QQ", layout, 8)
        buckets = struct.unpack_from("<Q", layout, 24)[0]
        mask = struct.unpack_from("<Q", layout, 48)[0]
        if not 1 <= count <= 100000 or not 1 <= mask <= 65535 or (mask + 1) & mask:
            return None
        hash_value = _fnv1a64(wxid)
        bucket_address = buckets + (hash_value & mask) * 16
        bucket = read(bucket_address, 16)
        first, node = struct.unpack("<QQ", bucket)
        visited = set()
        for _ in range(min(count, 256)):
            if node == sentinel or node in visited:
                break
            visited.add(node)
            if string(node + 0x10, 127) == wxid:
                contact = _pointer(ptr(node + 0x30))
                if string(contact + 8, 127) != wxid:
                    return None
                name = _clean_contact_name(string(contact + 0xD8))
                if not name:
                    return None
                if (ptr(node + 0x30) != contact or string(node + 0x10, 127) != wxid or
                        read(cache + 0xC8, 0x38) != layout or read(bucket_address, 16) != bucket or
                        ptr(context + 0x460) != provider or read_current_account(read_memory, core_base) != account):
                    return None
                return name
            if node == first:
                break
            node = ptr(node + 8)

        # 非好友群成员缓存于 ContactCache + 0x118 的 LRU unordered_map（node+0x30 -> lru_node -> +0x30 Contact*）。
        lru_layout = read(cache + 0x118, 0x38)
        lru_sentinel, lru_count = struct.unpack_from("<QQ", lru_layout, 8)
        lru_buckets = struct.unpack_from("<Q", lru_layout, 24)[0]
        lru_mask = struct.unpack_from("<Q", lru_layout, 48)[0]
        if not 1 <= lru_count <= 100000 or not 1 <= lru_mask <= 65535 or (lru_mask + 1) & lru_mask:
            return None
        lru_bucket_address = lru_buckets + (hash_value & lru_mask) * 16
        lru_bucket = read(lru_bucket_address, 16)
        lru_first, lru_map_node = struct.unpack("<QQ", lru_bucket)
        lru_visited = set()
        for _ in range(min(lru_count, 256)):
            if lru_map_node == lru_sentinel or lru_map_node in lru_visited:
                return None
            lru_visited.add(lru_map_node)
            if string(lru_map_node + 0x10, 127) == wxid:
                lru_node = _pointer(ptr(lru_map_node + 0x30))
                if string(lru_node + 0x10, 127) != wxid:
                    return None
                contact = _pointer(ptr(lru_node + 0x30))
                if string(contact + 8, 127) != wxid:
                    return None
                name = _clean_contact_name(string(contact + 0xD8))
                if not name:
                    return None
                if (ptr(lru_map_node + 0x30) != lru_node or ptr(lru_node + 0x30) != contact or
                        string(lru_map_node + 0x10, 127) != wxid or read(cache + 0x118, 0x38) != lru_layout or
                        read(lru_bucket_address, 16) != lru_bucket or ptr(context + 0x460) != provider or
                        read_current_account(read_memory, core_base) != account):
                    return None
                return name
            if lru_map_node == lru_first:
                return None
            lru_map_node = ptr(lru_map_node + 8)
        return None
    except (OSError, ValueError, struct.error):
        return None


def read_chatroom_members(read_memory, core_base, room_wxid, *, expected_account=None):
    """只读读取 ChatroomCache 中指定群的成员 wxid 列表；缓存未命中或发生变化时返回空元组。"""
    if not isinstance(room_wxid, str) or not room_wxid.endswith("@chatroom") or not room_wxid.isascii():
        return ()
    account = read_current_account(read_memory, core_base)
    if not account or (expected_account is not None and account != expected_account):
        return ()
    try:
        def read(address, length):
            value = read_memory(_pointer(address), length)
            if len(value) != length:
                raise ValueError("群成员缓存读取不完整")
            return value

        def ptr(address):
            return struct.unpack("<Q", read(address, 8))[0]

        def string(address, limit=127):
            header = read(address, 32)
            length, capacity = struct.unpack_from("<QQ", header, 16)
            if not 1 <= length <= limit or capacity < length or not (capacity == 15 or 16 <= capacity <= 1048576):
                raise ValueError("群成员字符串布局不匹配")
            buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
            body = read(buffer, length)
            if read(address, 32) != header or read(buffer, length) != body:
                raise ValueError("群成员字符串发生变化")
            return body.decode("utf-8")

        app = ptr(core_base + APP_GLOBAL_RVA)
        context = ptr(app + 0x68)
        table = ptr(context)
        if ptr(table + 0x38) != core_base + CACHE_PROVIDER_GETTER_RVA:
            return ()
        provider = _pointer(ptr(context + 0x460))
        if ptr(provider) != core_base + CACHE_PROVIDER_VTABLE_RVA:
            return ()
        registry = read(provider + 0x90, 16)
        head, size = struct.unpack("<QQ", registry)
        if not 1 <= size <= 512:
            return ()
        node = ptr(head)
        visited, chatroom_cache = set(), None
        for _ in range(size):
            if node == head or node in visited:
                break
            visited.add(node)
            value = ptr(node + 0x30)
            if value and ptr(value) == core_base + CHATROOM_CACHE_VTABLE_RVA:
                chatroom_cache = value
                break
            node = ptr(node)
        if not chatroom_cache or read(provider + 0x90, 16) != registry:
            return ()

        layout = read(chatroom_cache + 0x48, 0x38)
        sentinel, count = struct.unpack_from("<QQ", layout, 8)
        buckets = struct.unpack_from("<Q", layout, 24)[0]
        mask = struct.unpack_from("<Q", layout, 48)[0]
        if not 1 <= count <= 10000 or not 1 <= mask <= 65535 or (mask + 1) & mask:
            return ()
        hash_value = _fnv1a64(room_wxid)
        bucket_address = buckets + (hash_value & mask) * 16
        bucket = read(bucket_address, 16)
        first, room_node = struct.unpack("<QQ", bucket)
        room_visited, chatroom_obj = set(), None
        for _ in range(min(count, 256)):
            if room_node == sentinel or room_node in room_visited:
                break
            room_visited.add(room_node)
            if string(room_node + 0x10, 127) == room_wxid:
                chatroom_obj = _pointer(ptr(room_node + 0x30))
                break
            if room_node == first:
                break
            room_node = ptr(room_node + 8)
        if not chatroom_obj:
            return ()

        member_layout = read(chatroom_obj + 0xD0, 0x38)
        member_sentinel, member_count = struct.unpack_from("<QQ", member_layout, 8)
        if not 1 <= member_count <= 5000:
            return ()
        cur = ptr(member_sentinel)
        member_visited, members = set(), []
        for _ in range(member_count):
            if cur == member_sentinel or cur in member_visited:
                return ()
            member_visited.add(cur)
            wxid = string(cur + 0x10, 127)
            validate_account(wxid)
            members.append(wxid)
            cur = ptr(cur)
        if (read(chatroom_obj + 0xD0, 0x38) != member_layout or
                read(chatroom_cache + 0x48, 0x38) != layout or
                read_current_account(read_memory, core_base) != account):
            return ()
        return tuple(members)
    except (OSError, ValueError, struct.error):
        return ()

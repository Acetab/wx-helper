"""默认微信名读取和缓存变化验证，不连接微信。"""
import struct
import unittest

from test_account import AccountMemory, BASE, CONTEXT, SELF
from wxbot_framework.account import CONTEXT_VTABLE_RVA, IDENTITY_GETTER_RVA
from wxbot_framework.contacts import (CACHE_PROVIDER_GETTER_RVA, CACHE_PROVIDER_VTABLE_RVA,
                                     CHATROOM_CACHE_VTABLE_RVA, CONTACT_CACHE_VTABLE_RVA,
                                     read_chatroom_members, read_contact_name)

PEER = 'wxid_contact_name'
LRU_PEER = 'wxid_lru_member'
PROVIDER, HEAD, NODE, CACHE, SENTINEL, BUCKETS, ENTRY, CONTACT, STRINGS = [
    0x600000000 + i * 0x1000 for i in range(9)]


class ContactMemory(AccountMemory):
    def __init__(self, name='DemoUser'):
        super().__init__()
        self.add(BASE + CONTEXT_VTABLE_RVA, 0x80)
        self.write(BASE + CONTEXT_VTABLE_RVA + 0x18, struct.pack('<Q', BASE + IDENTITY_GETTER_RVA))
        self.add(BASE + CACHE_PROVIDER_GETTER_RVA + 28, 8)
        self.write(BASE + CACHE_PROVIDER_GETTER_RVA + 28, bytes.fromhex('48 83 b9 60 04 00 00 00'))
        self.add(CONTEXT + 0x460, 8)
        self.write(CONTEXT + 0x460, struct.pack('<Q', PROVIDER))
        self.write(BASE + CONTEXT_VTABLE_RVA + 0x38, struct.pack('<Q', BASE + CACHE_PROVIDER_GETTER_RVA))
        for address, length in ((PROVIDER, 0xC0), (HEAD, 0x40), (NODE, 0x40), (CACHE, 0x200),
                                (SENTINEL, 0x40), (BUCKETS, 32), (ENTRY, 0x40), (CONTACT, 0x100), (STRINGS, 1024)):
            self.add(address, length)
        self.q(PROVIDER, BASE + CACHE_PROVIDER_VTABLE_RVA)
        self.q(PROVIDER + 0x90, HEAD)
        self.q(PROVIDER + 0x98, 1)
        self.q(HEAD, NODE)
        self.q(NODE, HEAD)
        self.q(NODE + 0x30, CACHE)
        self.q(CACHE, BASE + CONTACT_CACHE_VTABLE_RVA)
        self.q(CACHE + 0xD0, SENTINEL)
        self.q(CACHE + 0xD8, 1)
        self.q(CACHE + 0xE0, BUCKETS)
        self.q(CACHE + 0xF8, 1)
        hashed = 0xCBF29CE484222325
        for byte in PEER.encode():
            hashed = ((hashed ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        for slot in (0, 16):
            self.write(BUCKETS + slot, struct.pack('<QQ', SENTINEL, SENTINEL))
        self.write(BUCKETS + (hashed & 1) * 16, struct.pack('<QQ', ENTRY, ENTRY))
        self.q(ENTRY + 0x30, CONTACT)
        self.s(ENTRY + 0x10, PEER, STRINGS)
        self.s(CONTACT + 8, PEER, STRINGS + 128)
        self.s(CONTACT + 0xD8, name, STRINGS + 256)

    def q(self, address, value):
        self.write(address, struct.pack('<Q', value))

    def s(self, address, value, buffer):
        body = value.encode('utf-8')
        if len(body) <= 15:
            self.write(address, body.ljust(16, b'\0') + struct.pack('<QQ', len(body), 15))
        else:
            self.write(buffer, body)
            self.write(address, struct.pack('<QQQQ', buffer, 0, len(body), 511))


class ContactTests(unittest.TestCase):
    def test_verified_id_returns_wechat_name_for_inline_and_unicode_heap_strings(self):
        for name in ('DemoUser', '微信原始名字🌸很长'):
            ram = ContactMemory(name)
            self.assertEqual(read_contact_name(ram.read, BASE, PEER, expected_account=SELF), name)

    def test_other_account_and_unknown_member_never_get_someone_elses_name(self):
        ram = ContactMemory()
        self.assertIsNone(read_contact_name(ram.read, BASE, PEER, expected_account='wxid_other'))
        self.assertIsNone(read_contact_name(ram.read, BASE, 'wxid_missing'))
        ram.s(CONTACT + 8, 'wxid_other', STRINGS + 128)
        self.assertIsNone(read_contact_name(ram.read, BASE, PEER))

    def test_wrong_layout_and_unavailable_cache_are_skipped(self):
        for address, value in ((PROVIDER, BASE + 1234), (CACHE, BASE + 5678),
                               (CACHE + 0xF8, 123), (CONTEXT + 0x460, 0)):
            ram = ContactMemory()
            ram.q(address, value)
            self.assertIsNone(read_contact_name(ram.read, BASE, PEER))

    def test_cache_changes_during_read_do_not_return_a_torn_name(self):
        ram = ContactMemory()
        changed = False
        def changing(address, size):
            nonlocal changed
            result = ram.read(address, size)
            if address == CONTACT + 0xD8 and not changed:
                changed = True
                ram.q(CACHE + 0xD8, 2)
            return result
        self.assertIsNone(read_contact_name(changing, BASE, PEER))
        self.assertEqual(read_contact_name(ram.read, BASE, PEER), 'DemoUser')

    def test_empty_and_control_character_names_are_rejected(self):
        for name in ('', '\u3164', '\u200b\u3164', '错误\n昵称', 'x' * 65):
            ram = ContactMemory(name)
            self.assertIsNone(read_contact_name(ram.read, BASE, PEER))

    def test_non_friend_lru_cache_returns_group_member_nickname(self):
        ram = ContactMemory('FriendUser')
        lru_sentinel, lru_buckets, lru_map_entry, lru_list_node, lru_contact = [
            0x600010000 + i * 0x1000 for i in range(5)]
        for address, length in ((lru_sentinel, 0x40), (lru_buckets, 32), (lru_map_entry, 0x40),
                                (lru_list_node, 0x40), (lru_contact, 0x100)):
            ram.add(address, length)
        ram.q(CACHE + 0x120, lru_sentinel)
        ram.q(CACHE + 0x128, 1)
        ram.q(CACHE + 0x130, lru_buckets)
        ram.q(CACHE + 0x148, 1)
        hashed = 0xCBF29CE484222325
        for byte in LRU_PEER.encode():
            hashed = ((hashed ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        for slot in (0, 16):
            ram.write(lru_buckets + slot, struct.pack('<QQ', lru_sentinel, lru_sentinel))
        ram.write(lru_buckets + (hashed & 1) * 16, struct.pack('<QQ', lru_map_entry, lru_map_entry))
        ram.s(lru_map_entry + 0x10, LRU_PEER, STRINGS + 512)
        ram.q(lru_map_entry + 0x30, lru_list_node)
        ram.s(lru_list_node + 0x10, LRU_PEER, STRINGS + 640)
        ram.q(lru_list_node + 0x30, lru_contact)
        ram.s(lru_contact + 8, LRU_PEER, STRINGS + 768)
        ram.s(lru_contact + 0xD8, '群友昵称甲', STRINGS + 896)
        self.assertEqual(read_contact_name(ram.read, BASE, LRU_PEER, expected_account=SELF), '群友昵称甲')

    def test_chatroom_cache_reads_group_member_wxids(self):
        ram = ContactMemory('FriendUser')
        room = '12345678@chatroom'
        room_node, room_cache, room_sentinel, room_buckets, room_entry, chatroom_obj, m_sentinel, m1, m2 = [
            0x600020000 + i * 0x1000 for i in range(9)]
        for address, length in ((room_node, 0x40), (room_cache, 0x100), (room_sentinel, 0x40),
                                (room_buckets, 32), (room_entry, 0x40), (chatroom_obj, 0x200),
                                (m_sentinel, 0x40), (m1, 0x40), (m2, 0x40)):
            ram.add(address, length)
        ram.q(PROVIDER + 0x98, 2)
        ram.q(NODE, room_node)
        ram.q(room_node, HEAD)
        ram.q(room_node + 0x30, room_cache)
        ram.q(room_cache, BASE + CHATROOM_CACHE_VTABLE_RVA)
        ram.q(room_cache + 0x50, room_sentinel)
        ram.q(room_cache + 0x58, 1)
        ram.q(room_cache + 0x60, room_buckets)
        ram.q(room_cache + 0x78, 1)
        hashed = 0xCBF29CE484222325
        for byte in room.encode():
            hashed = ((hashed ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        for slot in (0, 16):
            ram.write(room_buckets + slot, struct.pack('<QQ', room_sentinel, room_sentinel))
        ram.write(room_buckets + (hashed & 1) * 16, struct.pack('<QQ', room_entry, room_entry))
        ram.s(room_entry + 0x10, room, STRINGS + 512)
        ram.q(room_entry + 0x30, chatroom_obj)
        ram.q(chatroom_obj + 0xD8, m_sentinel)
        ram.q(chatroom_obj + 0xE0, 2)
        ram.q(m_sentinel, m1)
        ram.q(m1, m2)
        ram.q(m2, m_sentinel)
        ram.s(m1 + 0x10, PEER, STRINGS + 640)
        ram.s(m2 + 0x10, LRU_PEER, STRINGS + 768)
        self.assertEqual(read_chatroom_members(ram.read, BASE, room, expected_account=SELF), (PEER, LRU_PEER))

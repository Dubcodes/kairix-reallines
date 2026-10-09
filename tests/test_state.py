import json
import tempfile
import unittest
from pathlib import Path

from app.state import StateStore


class SilentDebug:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class RecordingClient:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class StoreCase(unittest.IsolatedAsyncioTestCase):
    def make_store(self, path):
        return StateStore(Path(path), SilentDebug())

    async def test_transient_item_update_broadcasts_without_layout_save(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            client = RecordingClient(); store.clients.add(client)
            item = store.scene['items'][0]
            path = store._layout_path(store.layout_index['active_id'])
            before = path.read_text()
            key = 'x1' if item['type'] == 'line' else 'x'
            await store.update_item(item['id'], {key: 4.25}, persist=False)
            self.assertEqual(client.messages[-1]['data']['scene']['items'][0][key], 4.25)
            self.assertEqual(path.read_text(), before)
            self.assertTrue(store.layout_dirty)
            await store.update_item(item['id'], {key: 5.5})
            self.assertEqual(json.loads(path.read_text())['items'][0][key], 5.5)
            self.assertFalse(store.layout_dirty)

    async def test_group_membership_visibility_translation_and_single_scene_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            group = await store.add_group('Officials')
            first, second = store.scene['items'][:2]
            await store.update_item(first['id'], {'group_id': group['id']})
            await store.update_item(second['id'], {'group_id': group['id'], 'visible': False})
            current = next(g for g in store.scene['groups'] if g['id'] == group['id'])
            self.assertEqual(current['item_ids'], [first['id'], second['id']])
            await store.update_group(group['id'], {'visible': False})
            self.assertFalse(current['visible'])
            self.assertTrue(first['visible'])
            self.assertFalse(second['visible'])

            writes = []
            original = store._atomic_write_path
            def counting(path, data):
                writes.append(path)
                original(path, data)
            store._atomic_write_path = counting
            before_x = first.get('x1', first.get('x'))
            before_z = first.get('z1', first.get('z'))
            await store.translate_group(group['id'], 2.0, -3.0)
            self.assertEqual(first.get('x1', first.get('x')), before_x + 2.0)
            self.assertEqual(first.get('z1', first.get('z')), before_z)
            self.assertEqual(sum(path == store._layout_path(store.layout_index['active_id']) for path in writes), 1)

    async def test_item_and_group_duplication_use_new_ids_and_keep_membership(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            group = store.scene['groups'][0]
            source = store.scene['items'][0]
            duplicate = await store.duplicate_item(source['id'])
            self.assertNotEqual(duplicate['id'], source['id'])
            self.assertIn(duplicate['id'], group['item_ids'])
            group_copy = await store.duplicate_group(group['id'])
            self.assertNotEqual(group_copy['id'], group['id'])
            self.assertTrue(group_copy['item_ids'])
            self.assertTrue(set(group_copy['item_ids']).isdisjoint(group['item_ids']))

    async def test_layout_lifecycle_and_switching(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            original_id = store.layout_index['active_id']
            copied = await store.create_layout('Track Test', copy_current=True)
            self.assertEqual(store.layout_index['active_id'], copied['id'])
            marker = await store.add_item('marker')
            await store.rename_layout(copied['id'], 'Track Test Renamed')
            duplicated = await store.duplicate_layout(copied['id'], 'Track Backup')
            self.assertEqual(store.layout_index['active_id'], duplicated['id'])
            await store.load_layout(original_id)
            self.assertFalse(any(item['id'] == marker['id'] for item in store.scene['items']))
            await store.load_layout(copied['id'])
            self.assertTrue(any(item['id'] == marker['id'] for item in store.scene['items']))
            self.assertTrue(await store.delete_layout(duplicated['id']))
            self.assertIsNone(store._layout_meta(duplicated['id']))

    async def test_legacy_scene_is_migrated_without_deleting_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = {'background': '#123456', 'items': [{'id': 'old', 'type': 'marker', 'name': 'Old', 'visible': True, 'color': '#fff', 'size': .5, 'label': 'Old', 'show_label': True, 'x': 1, 'y': 2, 'z': 0}]}
            (root / 'scene.json').write_text(json.dumps(legacy))
            store = self.make_store(directory)
            self.assertTrue((root / 'scene.json').exists())
            self.assertEqual(store.scene['items'][0]['id'], 'old')
            self.assertEqual(store.scene['groups'], [])
            second = self.make_store(directory)
            self.assertEqual(second.layout_index['active_id'], store.layout_index['active_id'])

    async def test_persistence_failure_keeps_working_scene_dirty_and_old_file_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(directory)
            item = store.scene['items'][0]
            key = 'x1' if item['type'] == 'line' else 'x'
            layout_path = store._layout_path(store.layout_index['active_id'])
            before = json.loads(layout_path.read_text())
            original = store._atomic_write_path
            def failing(path, data):
                if path == layout_path:
                    raise OSError('simulated full disk')
                original(path, data)
            store._atomic_write_path = failing
            with self.assertRaises(OSError):
                await store.update_item(item['id'], {key: 99})
            self.assertEqual(item[key], 99)
            self.assertTrue(store.layout_dirty)
            self.assertIn('simulated full disk', store.layout_save_error)
            self.assertEqual(json.loads(layout_path.read_text()), before)


if __name__ == '__main__':
    unittest.main()

"""V1.6E safety regressions: single publication clock and Zoom edit integrity."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.tracking_consumer import adopt, dedicated_pose_consumer_loop


class FakeHistory:
    def __init__(self): self.resets=[]
    def reset(self, reason): self.resets.append(reason)


class FakeStore:
    def __init__(self):
        self.lock=asyncio.Lock()
        self.tracking={'valid':True,'axes':{}}
        self.camera={'valid':True,'world_valid':False}
        self.zoom_fov_mapping={'wide':{'relative_count':12},'tight':None}
        self.pose_history=FakeHistory()
        self._pose_reference_key=None
        self._dedicated_config_syncing=0
        self.sample_count=0
    def _refresh_pose_reference_and_sample(self): self.sample_count+=1


class AdoptTests(unittest.TestCase):
    def test_zoom_edit_not_overwritten_during_ack(self):
        store=FakeStore();store._dedicated_config_syncing=1
        state={'producer_id':22,'generation':1,'tracking':store.tracking,
               'camera':store.camera,'zoom_fov':{'wide':None,'tight':None}}
        adopt(store,state,None)
        self.assertEqual(store.zoom_fov_mapping['wide']['relative_count'],12)
        store._dedicated_config_syncing=0
        adopt(store,{**state,'generation':2},(22,1))
        self.assertIsNone(store.zoom_fov_mapping['wide'])

    def test_duplicate_owner_generation_is_not_new_pose(self):
        store=FakeStore()
        state={'producer_id':22,'generation':1,'tracking':store.tracking,
               'camera':store.camera,'zoom_fov':store.zoom_fov_mapping}
        identity=adopt(store,state,None)
        adopt(store,state,identity)
        self.assertEqual(store.sample_count,1)


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_publish_per_consumer_cycle(self):
        store=FakeStore()
        state={'producer_id':44,'generation':1,'tracking':store.tracking,
               'camera':store.camera,'zoom_fov':store.zoom_fov_mapping}
        class Proxy:
            reads=0
            def latest(self):
                self.reads+=1
                return {**state,'generation':self.reads}
        proxy=Proxy(); published=[]
        async def publish(s):
            published.append(s.sample_count)
            if len(published)>=3:
                raise asyncio.CancelledError
        with patch('app.tracking_consumer.publish_pose_once',publish):
            with self.assertRaises(asyncio.CancelledError):
                await dedicated_pose_consumer_loop(store,proxy,rate_hz=1000)
        self.assertEqual(proxy.reads,3)
        self.assertEqual(published,[1,2,3])


if __name__=='__main__':unittest.main()

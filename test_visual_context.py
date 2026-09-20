import base64,io,unittest
from PIL import Image
from visual_context import VisualContext
from omni_backend import ConversationHistory

def frame(number=1):
 b=io.BytesIO();Image.new('RGB',(20,20),'red').save(b,format='PNG')
 return dict(type='visual',session_id='session-a',sequence=number,operation='set',image=dict(id=f'image-{number}',source='upload',captured_at_ms=number*100,data=base64.b64encode(b.getvalue()).decode()))

class VisualTests(unittest.TestCase):
 def test_session_sequence_and_bad_data_do_not_replace_pending(self):
  v=VisualContext('session-a',enabled=True,clock=lambda:10)
  ack=v.control(frame());self.assertEqual(ack['state'],'pending')
  for bad in [dict(frame(2),session_id='old'),frame(),dict(frame(2),image=dict(frame(2)['image'],data='!'))]:
   with self.assertRaises(ValueError):v.control(bad)
  user,event=v.bind({'role':'user','text':'what'})
  self.assertEqual(user['images'][0]['id'],'image-1');self.assertEqual(event['turn_id'],1)
  user,event=v.bind({'role':'user','text':'again'});self.assertNotIn('images',user)
 def test_snapshot_clear_and_disabled(self):
  v=VisualContext('session-a',enabled=True);x=frame();v.control(x);x['image']['id']='mutated'
  user,_=v.bind({'role':'user','audio':'AAA='});self.assertEqual(user['images'][0]['id'],'image-1')
  v.control(frame(2));v.control(dict(type='visual',operation='clear',sequence=3,session_id='session-a'))
  self.assertNotIn('images',v.bind({'role':'user','text':'x'})[0])
  with self.assertRaises(ValueError):VisualContext('session-a',enabled=False).control(frame())
 def test_history_retains_two_images_and_marks_omission(self):
  h=ConversationHistory()
  for n in range(1,4):
   messages=h.begin(dict(role='user',text='what',images=[frame(n)['image']]))
   h.generated('answer');h.heard()
  self.assertEqual(sum(len(m.get('images',[])) for m in messages),2)
  self.assertTrue(messages[0]['images_omitted']);self.assertTrue(h.trimmed)
  self.assertEqual([m['images'][0]['id'] for m in messages if 'images' in m],['image-2','image-3'])

class VisualSerializerTests(unittest.IsolatedAsyncioTestCase):
 async def test_only_visual_controls_get_large_message_budget(self):
  import json
  from avatar_session import AvatarSerializer
  serializer=AvatarSerializer();message=frame();message['image']['data']='A'*10000
  self.assertIsNotNone(await serializer.deserialize(json.dumps(message)))
  self.assertIsNone(await serializer.deserialize(json.dumps(dict(type='text',text='x'*5000))))
  message['image']['data']='A'*360001
  self.assertIsNone(await serializer.deserialize(json.dumps(message)))

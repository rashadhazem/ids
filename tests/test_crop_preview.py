import io
import re
import os
import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import base64
from PIL import Image
from app import app

client = app.test_client()

# Fetch CSRF token from login page
res_get = client.get('/auth/login')
html = res_get.get_data(as_text=True)
match = re.search(r'name="csrf_token" value="([^"]+)"', html)
csrf_token = match.group(1) if match else ''

# Mock logged-in session
with client.session_transaction() as sess:
    sess['user_id'] = 1
    sess['role'] = 'superadmin'

real_path = os.path.join(os.path.dirname(__file__), '..', 'static', 'uploads', '2023', 'كلية_العلاج_الطبيعي', '2023006972.jpg')
with open(real_path, 'rb') as f:
    img_data = f.read()

res = client.post(
    '/api/crop-preview',
    headers={'X-CSRFToken': csrf_token},
    data={'image': (io.BytesIO(img_data), 'test.jpg')}
)

print('Status code:', res.status_code)
assert res.status_code == 200, f'Failed with status {res.status_code}: {res.get_data(as_text=True)}'
data = res.get_json()
assert data['success'] is True
assert 'data:image/jpeg;base64,' in data['preview']

b64_str = data['preview'].split(',')[1]
decoded_bytes = base64.b64decode(b64_str)
im = Image.open(io.BytesIO(decoded_bytes))
print('Preview image size:', im.size)
assert im.size == (400, 500), f'Unexpected size: {im.size}'

print('SUCCESS: /api/crop-preview generates exact 400x500 professional face crop!')

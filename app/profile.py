"""当前账号头像：校验并重新编码图片，原始文件及元数据不进入存储。"""
from hashlib import sha256
from io import BytesIO

from fastapi import File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response
from PIL import Image, ImageOps, UnidentifiedImageError

from app.repository import now_iso

AVATAR_TABLE = """
CREATE TABLE product_user_avatars (
    user_id TEXT PRIMARY KEY REFERENCES product_users(id) ON DELETE CASCADE,
    image BLOB NOT NULL,
    revision TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
MAX_BYTES = 2 * 1024 * 1024
MAX_DIMENSION = 4096
PRIVATE_HEADERS = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'}


def normalize_avatar(data: bytes) -> bytes:
    if not data or len(data) > MAX_BYTES:
        raise HTTPException(422, '头像图片须小于或等于2MB')
    try:
        # 只启用三种解码器；不相信扩展名/MIME，不保存可执行SVG或任意原文件。
        with Image.open(BytesIO(data), formats=('PNG', 'JPEG', 'WEBP')) as image:
            if max(image.size) > MAX_DIMENSION or min(image.size) < 1:
                raise HTTPException(422, '图片宽高均须不超过4096像素')
            if getattr(image, 'n_frames', 1) != 1:
                raise HTTPException(422, '请选择静态图片作为头像')
            image.load()
            upright = ImageOps.exif_transpose(image).convert('RGBA')
            cropped = ImageOps.fit(upright, (256, 256), method=Image.Resampling.LANCZOS)
            # 新建像素图，去掉EXIF、文本、ICC等输入元数据。
            clean = Image.new('RGBA', cropped.size)
            clean.paste(cropped)
            output = BytesIO()
            clean.save(output, format='PNG')
            return output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, '无法读取图片，请选择有效的PNG、JPG或WebP静态图片') from exc


def install_profile_routes(app, access):
    def owner():
        if not access.actor:
            raise HTTPException(401, '请登录后设置个人头像')
        return access.actor['id']

    def profile(user_id):
        with access.repository.connect() as db:
            row = db.execute('SELECT revision FROM product_user_avatars WHERE user_id=?', (user_id,)).fetchone()
        return {'display_name': access.actor['display_name'],
                'avatar_url': '/api/auth/avatar?v=' + row['revision'] if row else None}

    @app.get('/api/auth/profile')
    def get_profile():
        return JSONResponse(profile(owner()), headers=PRIVATE_HEADERS)

    @app.put('/api/auth/avatar')
    def upload_avatar(file: UploadFile = File(...)):
        user_id = owner()
        try:
            image = normalize_avatar(file.file.read(MAX_BYTES + 1))
        finally:
            file.file.close()
        with access.repository.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM product_users WHERE id=? AND active=1', (user_id,)).fetchone():
                raise HTTPException(401, '账号状态已变更，请重新登录')
            db.execute('INSERT INTO product_user_avatars VALUES (?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
                       'image=excluded.image,revision=excluded.revision,updated_at=excluded.updated_at',
                       (user_id, image, sha256(image).hexdigest(), now_iso()))
        return JSONResponse(profile(user_id), headers=PRIVATE_HEADERS)

    @app.get('/api/auth/avatar')
    def get_avatar():
        user_id = owner()
        with access.repository.connect() as db:
            row = db.execute('SELECT image FROM product_user_avatars WHERE user_id=?', (user_id,)).fetchone()
        if not row:
            raise HTTPException(404, '尚未设置头像')
        return Response(bytes(row['image']), media_type='image/png', headers=PRIVATE_HEADERS)

    @app.delete('/api/auth/avatar')
    def reset_avatar():
        user_id = owner()
        with access.repository.connect() as db:
            db.execute('DELETE FROM product_user_avatars WHERE user_id=?', (user_id,))
        return JSONResponse(profile(user_id), headers=PRIVATE_HEADERS)

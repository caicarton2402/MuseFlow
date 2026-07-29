# MuseFlow

一个合规优先的微信公众号授权图片收录与沉浸式推荐 MVP。

## 功能

- 导入用户明确授权的 `https://mp.weixin.qq.com` 公开文章。
- 解析正文图片，限制域名、响应体积和图片尺寸，阻止 SSRF 与异常资源。
- SHA-256 去重，图片转换为 WebP 后本地保存，SQLite 保存来源和特征。
- 手机端上下滑动、PC 滚轮/键盘逐屏切换，布局自动适配。
- 推荐融合三类信号：
  - 视觉：RGB 色彩直方图、亮度、对比度、纵横比等视觉特征；
  - 文本：文章标题、公众号名称、图片描述的哈希语义特征；
  - 行为：停留时长、喜欢、不喜欢、快速跳过形成用户向量。
- 冷启动的新鲜度/竖图偏好、探索噪声、来源多样性与最近已看降权。
- 图片状态支持 `approved` / `pending` / `blocked`；自动发现的图片默认进入待审核队列，不会直接展示。
- `discover.py` 接收公开搜索发现的文章链接，只有公众号名称和微信唯一 `biz` 标识均匹配授权清单时才会处理。

## 合规边界

本项目不提供批量扫描公众号、绕过登录、验证码、付费墙或反爬的能力。导入时必须确认：

1. 对来源内容拥有采集、存储及展示授权；
2. 图片中的人物均为成年人；
3. 展示页面保留文章来源链接；
4. 正式上线前补充内容安全审核、版权投诉/删除机制、隐私政策和用户协议。

## 启动

需要 Python 3.11+，建议使用 VWork 内置 `uv`：

```bash
uv venv
uv pip install -r requirements.txt
uv run uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。

如需改变数据目录：

```powershell
$env:BEAUTY_FEED_DATA="D:\museflow-data"
uv run uvicorn app:app --host 127.0.0.1 --port 8000
```

## 自动发现

已授权公众号及唯一标识维护在 `discover.py` 的 `AUTHORIZED_SOURCES`。当前清单包括：醉青春壁纸库、泡芙喵头像社、开心需要理由吗、热头像、会画卧蚕吗。发现程序本身不扫描微信私有接口，也不绕过登录或验证码；它只接收公开搜索引擎返回的候选文章 URL，并再次打开文章核验来源。

```powershell
uv run --with-requirements requirements.txt python discover.py `
  "https://mp.weixin.qq.com/s/候选文章1" `
  "https://mp.weixin.qq.com/s/候选文章2"
```

新图片保存为 `pending`，不会出现在推荐流。审核接口：

- `GET /api/moderation/pending`：列出待审核图片；
- `GET /api/moderation/media/{id}`：查看待审核预览；
- `PATCH /api/images/{id}/moderation`，传入 `approved` 或 `blocked`。

文章 HTML 上限默认 12MB，可通过 `MUSEFLOW_MAX_ARTICLE_BYTES` 调整；单张图片上限仍固定为 15MB。

## 测试

```bash
uv run python -m unittest -v test_app.py
```

## 主要接口

- `GET /health`：服务与收录数量。
- `GET /api/feed?user_id=...&limit=12`：个性化图片流。
- `POST /api/interactions`：记录 `view`、`like`、`dislike`、`skip`。
- `POST /api/ingest`：导入一篇授权公众号文章。
- `GET /api/moderation/pending`：获取自动发现产生的待审核队列。
- `GET /api/moderation/media/{id}`：仅供本机审核待审核图片。
- `PATCH /api/images/{id}/moderation`：设置 `approved`、`pending` 或 `blocked`。

## 生产化建议

- 将本地媒体迁移至对象存储/CDN，SQLite 替换为 PostgreSQL。
- 异步队列执行导入、转码、内容审核和向量计算。
- 使用 CLIP/SigLIP 生成更强的图文统一向量，并用 pgvector/Milvus 做召回。
- 增加登录、设备合并、曝光日志、A/B 测试、推荐指标与可解释性。
- 对接合规的内容安全服务，默认先审后发；建立举报、下架与版权申诉流程。

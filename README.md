# tripo2api

把 **Tripo AI**（[studio.tripo3d.ai](https://studio.tripo3d.ai)）的**图像生成**与**图生 3D**能力封装为可编程 HTTP 接口，并自带一个单文件网页控制台。

纯 Python 标准库实现（外加 `certifi` / `websocket-client` 两个可选依赖），**无框架、无 npm**。

> **协议来源**：Tripo 没有开放官方 API，本文的所有端点均通过**浏览器抓包 + 前端 bundle 静态分析**还原，并逐条实测验证（证据分级：Confirmed / Strong Evidence / Hypothesis）。
> 完整探测报告见 [`sessions/2026-10-05-tripo-studio/RECON.md`](https://github.com/Zhengyuuuui/tripo2api/blob/main/docs/RECON.md)。

---

## 能做什么

| 能力 | 状态 | 实测成本 |
|--|--|--|
| **图像生成** | ✅ 可用 | 10 credits/张 |
| **图生 3D（GLB）** | ✅ 可用 | 55 credits/个 |
| 文生 3D | ❌ 会员功能（403 `6102`） | — |
| 多视图 / 分部件 / 8K 贴图 | ❌ 会员功能 | — |
| 格式导出（FBX/OBJ/STL/USD/3MF） | ❌ 会员功能（403 `6102`） | — |

**免费账号实测**：注册送 **200 credits**（月底过期），约合 **20 张图** 或 **3 个 3D 模型**。
另有两档免费次数：`smart_mesh_v2` ×2、`uv_edit` ×2（但 basic 计划下入口被会员门挡住，实际不可达）。

---

## 快速开始

```bash
git clone https://github.com/Zhengyuuuui/tripo2api.git
cd tripo2api
pip3 install certifi websocket-client     # websocket-client 仅"浏览器捕获登录态"需要

python3 serve.py                            # → http://127.0.0.1:4878
```

打开 <http://127.0.0.1:4878> 即是控制台。

**端口**：`TRIPO_PORT=4878 python3 serve.py`（默认 4878）。

---

## 账号从哪来

凭据存在 `data/tripo2api.db` 的 `accounts` 表（SQLite WAL）。

**唯一推荐方式：控制台「账号池」页 → 从 CDP 浏览器捕获**（不接触密码）：

1. 用 `--remote-debugging-port=9366` 启动 Chrome，登录 <https://studio.tripo3d.ai>
2. 控制台「账号池」页填端口 `9366` → 点「捕获登录态」

网关会连上你的 Chrome，读取 cookie 并换一张 ES256 JWT 入库。**只读已登录态，不碰密码**。

### 凭证构成（Tripo 用的是 Ory Kratos，不是 access/refresh 双 token）

```
ory_kratos_session cookie   30 天    ← 长期凭证，不可续，只能重新登录
Authorization: Bearer <JWT> 15 分钟   ← access token，可无限续签
```

必带 header：`authorization` / `x-tripo-device-id` / `x-tripo-start`（+ `csrf_token`）。

---

## 关键特性

### 1. Cloudflare 只挡网页，不挡 API

`studio.tripo3d.ai`（网页）有 Cloudflare Managed Challenge，`curl` 直连返回 403；
但 `api.tripo3d.ai`（API）**纯 HTTP 直连即可**，无需浏览器常驻、无需 TLS 指纹伪装。

> 实测：关闭全部 Tripo 标签页后，纯 HTTP 完成「提交 → 轮询 → 换签名 → 下载」全链路（1.4MB PNG 校验通过）。

### 2. 后台保活守护（关浏览器也生效）

`keepalive.py` 是一个独立后台线程（守护进程），做三件事：

- **JWT 提前续签** —— 每 300s 检查，剩余 < 300s 就换新，**不产生"首次请求要等一次 401"的往返**
- **额度快照刷新** —— 每 1800s 拉 credits / quota / 资产数写回 DB
- **故障指数退避** —— 连续失败 20s→40s→…（上限 300s），不会打爆上游

```bash
TRIPO_KEEPALIVE=1     # 开关（默认启用，设为 0 关闭）
TRIPO_RENEW_SEC=300   # JWT 检查间隔
TRIPO_RENEW_AHEAD_SEC=300  # 剩余不足多少秒时提前续签
TRIPO_CREDIT_SEC=1800 # 额度刷新间隔
TRIPO_WARN_DAYS=3     # session 临期预警阈值（天）
```

> ⚠️ **JWT 可全自动续签，但 session（30 天）不可续** —— 到期必须人工打开浏览器点一次「捕获」。

### 3. 产物零落盘

数据库**只存 `s3_bucket` + `s3_key`（永久有效）+ 元数据**，图片/模型字节一律不落盘。

签名 URL 是 CloudFront 预签名，**约 1~24 小时过期**，随用随取：

```
GET /v1/db/artifacts/fresh-url?kind=image&id=<asset_id>              → JSON {url, storage}
GET /v1/db/artifacts/fresh-url?kind=image&id=<asset_id>&download=1  → 302 直跳官方 CDN
```

> **实测发现**：`get_image_asset` / `project/detail` 每次调用都会**重新签名** —— 同一个产物两次取到的 URL 不同。所以不需要专门的"换签名"端点，现有查询端点本身就是签名服务。
> `&download=1` 返回 **302**，图片字节从 Tripo 服务器直出，**完全不经过本机**。

### 4. 账号池与配额聚合

并发拉取池内每个账号的 `assets/v2`，按归属聚合，输出每账号配额进度条（上限由服务端按订阅档下发，basic 档 = 10/账号）。

三种视图：`合并`（本地 DB + 云端去重）/ `云端实时` / `仅本地 DB`。

**预置模型**（`type=preset`，如 `Tripo Demo Model`）默认隐藏 —— 它们占用配额但不是你的产物。判定依据是 `type` 字段而非名称，官方改名也不会漏掉。

### 5. 自动入库 + 云端回填

经本代理的生成请求**自动落库**（提交写 running → 轮询到 success 补 url + s3_key），幂等键为 `asset_id` / `operator_id`。

在官网 UI 或直接调 API 生成的产物本地看不到，用「云端回填」补齐：

```
GET /v1/db/images/backfill?pages=5
→ {"ok":true,"added":5,"updated":1,"total_before":1,"total_after":6}
```

### 6. 图片上传（服务端代传 S3）

```
POST /v1/upload/image     multipart(file) 或 data_url
```

流程：`storage/temporary_token` 取 AWS STS 临时三元组 → 服务端用 `awssign.py`（纯标准库手写 SigV4）签 presigned PUT 直传 S3 → `audit/image` 拿 `image_audit_result`。

**STS 凭证不出服务端**。仅支持 PNG/JPEG/WebP，≤20MB。

---

## 控制台

单文件 `console.html`（无框架、无构建），GitHub dark 风格，8 个页面：

| 页面 | 内容 |
|--|--|
| **总览** | credits / 到期 / 资产 / 两档 quota / 导出额度 / 保活状态 / 成本速查 / 模型表 |
| **生图** | 9 模型 + 10 模板 + **参考图上传** + 比例/张数/TPose/草图 → 自动轮询出图 |
| **3D** | 图生 3D（模型/面数/四边面/PBR/几何质量）+ **实时进度条** + 产物下载 |
| **资产** | 我的资产（含导出通道矩阵与 meshopt 兼容性提示） |
| **价格** | 官方 11 个套餐实时价格 + 免费额度实测表 |
| **账号池** | 浏览器捕获 / 账号表 / **保活面板** / 凭证说明 |
| **产物** | 配额总览 + 统一产物表（合并/云端/本地三视图）+ 存储策略 |
| **日志** | 全量请求日志（带耗时与状态着色） |

> **3D 进度提示**：`progress=99%` 会卡 90~120 秒（贴图烘焙），此时 `left_time` 已接近 0 但 `status` 仍是 `running`。轮询器**必须等 `status=="success"`**，不能信 `left_time`。

---

## API

### 代理（透传上游，路径 `/v1/studio/*` → 上游 `/v2/studio/*`）

所有 Tripo 端点都可直接调，用于自定义脚本：

```bash
# 查额度
curl http://127.0.0.1:4878/v1/studio/user/profile/payment

# 3D 配额
curl -X POST -H 'content-type: application/json' -d '{}' \
     http://127.0.0.1:4878/v1/studio/operation/quota

# 生图
curl -X POST -H 'content-type: application/json' -d '{
  "if_upload":true,"sketch_to_render":false,"t_pose":false,"amount":1,
  "model_version":"gemini_2.5_flash_image_preview",
  "prompt":"a red apple on white background","resolution":"1K","scale":"1:1"
}' http://127.0.0.1:4878/v1/studio/image/gen_image_v2

# 轮询（提交返回的 asset_id）
curl -X POST -H 'content-type: application/json' \
     -d '{"asset_id":"<asset_id>"}' \
     http://127.0.0.1:4878/v1/studio/image/get_image_asset
```

### 账号池

| Method | Path | Description |
|--|--|--|
| GET | `/v1/accounts` | 账号列表（含额度快照 / JWT 状态） |
| POST | `/v1/accounts/capture` | 从 CDP 浏览器捕获 `{port?}` |
| POST | `/v1/accounts/refresh-credits` | 逐账号刷新额度（401 自动续签重试） |
| POST | `/v1/accounts/renew-jwt` | 手动续签 `{sub?}` |
| POST | `/v1/accounts/remove` | 移除 `{sub}` |
| GET | `/v1/accounts/export` | 导出（含完整凭证） |

### 产物

| Method | Path | Description |
|--|--|--|
| GET | `/v1/db/artifacts?source=merged\|cloud\|db` | 产物清单（`source` 三视图） |
| GET | `/v1/db/artifacts/fresh-url?kind=&id=` | 换新签名 URL |
| GET | `/v1/db/artifacts/fresh-url?...&download=1` | **302 直跳官方 CDN** |
| GET | `/v1/pool/assets` | 跨账号资产聚合 + 配额 |
| GET | `/v1/db/images/backfill?pages=N` | 云端回填生图记录 |
| GET | `/v1/db/images` · `/v1/db/models3d` · `/v1/db/stats` · `/v1/db/accounts` | 本地记录查询 |

### 保活

| Method | Path | Description |
|--|--|--|
| GET | `/v1/keepalive/status` | 守护状态（轮次/间隔/每账号 JWT 剩余/失败数/退避） |
| POST | `/v1/keepalive/tick` | 立即跑一轮 |

### 上传

| Method | Path | Description |
|--|--|--|
| POST | `/v1/upload/image` | 上传图片到 Tripo S3 → `{bucket,key,audit_result}` |

---

## 模型清单

### 图像（9 个）

| 模型 ID | 显示名 | 成本 | basic |
|--|--|--|--|
| `gemini_2.5_flash_image_preview` | Nano Banana | 10c | ✅ **唯一可用** |
| `gemini_3.1_flash_image_preview` | Nano Banana 2 | 10c | ❌ |
| `gemini_3_pro_image_preview` | Nano Banana Pro | 20c | ❌ |
| `gpt_image_2.5_sunburst` | GPT Image 2.5 | — | ❌ |
| `gpt_image_2` | GPT Image 2 | 20c | ❌ |
| `gpt_image_1.5` | GPT Image 1.5 | — | ❌ |
| `gpt_4o` | GPT-4o | — | ❌ |
| `flux.1_kontext_pro` | FLUX.1 Kontext Pro | 10c | ❌ |
| `flux.1_dev` | FLUX.1 Dev | — | ❌ |
| `midjourney` | Midjourney | 10c | ❌ |

> **为什么 basic 只有 1 个？** 前端 `memberPlans` 权限表里，Nano Banana 是
> `{Basic:true, Starter:true, Professional:true, ...}`，**其余全是 `Basic:false`**。
> 服务端**不会拦**（10 个 model_version 全部通过 schema 校验），是**前端 gate** 把它们藏起来了 —— UI 下拉里根本没有其它选项。

### 3D（7 个可用版本）

`v1.0-20250506` · `v2.0-20260430` · `v2.5-20260210` · `v3.0-20260909` · `v3.1-20260211` · `Nexus-v1.0-20260214` · `Nexus-v2.0-20260801`

> ⚠️ **`Tripo H3.1` / `P1.0` / `H2.5` 等是 UI 显示名，API 一律 `400 invalid modelVersion` 拒绝**。必须用日期版本号。

---

## 格式转换（交给本地工具）

网关拿到的 GLB 是 **Tripo 原生产物**（顶点/贴图与会员导出完全相同），只是不能转格式。

免费 GLB 使用 `EXT_meshopt_compression` + `KHR_mesh_quantization`：位置坐标仍是 float32（精度无损），**压缩比约 3.6×**（15.8MB vs ~56MB）。

| 工具 | 直接支持 meshopt？ |
|--|-----|
| Blender ≥ 3.0 | ✅ 内置 |
| three.js | ✅ `GLTFLoader` + `MeshoptDecoder` |
| Unity（GLTFast）/ Unreal 5 / Babylon.js | ✅ |
| `gltfpack` / `gltf-transform` CLI | ✅ |
| **3D 打印切片（PrusaSlicer / Cura）** | ❌ **必须先转 STL/OBJ** |

本地转换（**不花钱、无需会员**）：

```bash
# 解压 meshopt
npx @gltf-transform/cli optimize model.glb plain.glb

# 转 STL / OBJ
brew install assimp
assimp export model.glb model.obj
assimp export model.glb model.stl
```

> ⚠️ 朴素 glTF 解析器若忽略 `extensionsRequired` 会**静默读到空 buffer**（表现为"模型加载了但是空的"）。

---

## 关键实现说明

- **端点还原方式**：CDP 抓包（`Network.requestWillBeSent`）+ Nuxt bundle 静态分析（`_nuxt/*.js`，6.17MB / 118 chunk）。端点表 115 条见 `artifacts/endpoint-table.txt`。
- **代理路径映射**：console 用 `/v1/studio/*`（与其他 2api 项目一致），`serve.py` 内部映射到上游 `/v2/studio/*`。
- **`image_assets` 的字段名**是 `page_num` / `page_size`，列表在 `data.assets`（不是 `offset/limit` + `data.list`）。
- **生图参考图字段**是 `image: {bucket, key, image_audit_result, image_source}`，类型必须是 struct（传字符串报 `type mismatch`）。
- **`audit/image` 返回 `data.result`**（不是 `data.image_audit_result`）。
- **S3 SigV4 手写实现**（`awssign.py`，不依赖 boto3）：canonical query **必须按 key 字节序排序**，`X-Amz-Security-Token` 排在 `X-Amz-SignedHeaders` **之前**，否则 `SignatureDoesNotMatch`。
- **`download_with_name` 的 `file_name` 扩展名被服务端忽略** —— 传 `.stl`/`.obj`/`.fbx` 都返回同一个 GLB，**不存在免费格式转换路径**。
- **配额 `total` 含预置模型**：算用户可用配额要 `total - presets`。

---

## 项目结构

```
tripo2api/
├── serve.py          # HTTP 服务 + 上游代理 + 上传 + 产物入库（1271 行）
├── db.py             # SQLite 持久层：accounts/images/models_3d/generation_log
├── keepalive.py      # 后台保活守护（JWT 续签 / 额度刷新 / 指数退避）
├── awssign.py        # 最小 AWS SigV4 签名器（仅 S3 PUT，无 boto3）
├── console.html      # 单文件控制台（8 页，无框架）
├── config.example.json
└── data/tripo2api.db # SQLite（WAL），已 gitignore
```

---

## 安全说明

- `config.json` / `accounts.json` / `data/` **已 gitignore**，不要提交。
- `/v1/accounts/export` 返回**完整凭证**（含 cookie / JWT），仅绑定 `127.0.0.1`，勿暴露到公网。
- 服务只监听 `127.0.0.1`，无鉴权 —— 本机工具设计，不要直接对外。

---

## 已知限制

| 限制 | 说明 |
|--|--|
| 无 OpenAI 兼容接口 | 当前是 Tripo 原生端点代理；如需 `/v1/images/generations` 可自行包一层 |
| 资产上限 10/账号 | 服务端下发，basic 档；满了要删旧资产或升级 |
| 格式导出需会员 | 见「格式转换」一节 |
| session 30 天需人工 | JWT 能自动续，session 不能 |
| 单账号资产上限小 | 账号池可缓解（每个账号独立 10 个额度） |

---

## 免责声明

本项目仅用于**个人学习与互操作研究**，逆向分析的是你自己账号的浏览器流量。
请遵守 Tripo 的服务条款，合理使用额度。生成的模型/图片版权归属 Tripo 与原上传者。

---

## 相关

- 协议探测报告：[`docs/RECON.md`](https://github.com/Zhengyuuuui/tripo2api/blob/main/docs/RECON.md)
- 同类项目：[meshy2api](https://github.com/Zhengyuuuui/meshy2api)
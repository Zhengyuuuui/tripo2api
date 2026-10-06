# Tripo Studio (studio.tripo3d.ai) 探测报告

> 网址：https://studio.tripo3d.ai/zh/workspace/generate-image
> 探测日期：2026-10-05
> 探测者：opencode / search-probe skill
> 阶段：**已授权抓包**（用户明确许可："对这个app进行扫探测，我已经在9366的chrome上已经登录了"）
> 结论上限：**Confirmed**（已实测生成成功）

## 0. 一句话结论

**可行 —— 图像 + 3D 双双实测跑通。**
纯 HTTP（路线A）全链路打通：图像生成 200 credits 够用；**3D 图生模型实测成功并下载到
15.8MB 有效 GLB**（198 万三角面，gltfpack 1.2 压缩）。唯一阻塞 3D 的是 `text_to_model`
（文生 3D，UI 根本不提供输入框），`image_to_model` **不设防**。
官方 **OpenAPI 已存在**（`platform.tripo3d.ai/docs`）且更稳，但 Web 端私有端点已完整还原：
115 个 `/v2/studio/*` 端点 + 8 个图像模型 + 7 个可用 3D 版本，认证为 Ory Kratos 会话 + ES256 JWT。

## 1. 基本信息

| 项 | 值 | 证据等级 | 来源 |
|--|--|--|--|
| 形态 | Web 应用（Nuxt 3 SPA），另有官方开发者 API 平台 | Confirmed | `__NUXT_DATA__` payload、`_nuxt/builds/meta/*.json` |
| 能力 | **3D 模型生成**（文/图/多视图→mesh）、**图像生成/编辑**、**骨骼绑定**、**重拓扑**、**PBR 贴图**、**动捕**、**模型导出**（USD/GLB/OBJ） | Confirmed | bundle 端点表 + NUXT task-type 枚举 |
| 免费额度 | 账号实测 **200 credits**（`expiring_date 2026-11-01`）+ `smart_mesh_v2` 2 次 + `uv_edit` 2 次 + 免费导出 15 次 | **Confirmed** | `GET /v2/studio/user/profile/payment`、`POST /operation/quota`、`GET /marketing/export-limit` 原文 |
| 单价 | 实测 1 张图扣 **10 credits**（200→190） | Confirmed | 生成前后 wallet 对比 |
| 认证 | **Ory Kratos** 会话（邮箱验证码 code method，`aal1`）+ **ES256 JWT**（`iss=auth.tripo3d.ai`） | Confirmed | `GET /studio/whoami` 响应体、CDP 抓到的 Authorization 头 |
| 防护 | **Cloudflare Managed Challenge 只挡网页域**；`api.tripo3d.ai` 纯 HTTP 直连不拦 | Confirmed | curl 网页 403 `cf-mitigated: challenge`；curl API 200 |
| 上游模型 | 图像 8 个（Gemini/Nano Banana、GPT Image 2.5、FLUX.1、Midjourney）；3D 13 个（Tripo H2.5/H3.0/H3.1、P1.0/P2.0、Nexus v1.0/v2.0、v1.0~v3.1） | Confirmed | bundle enum + 实测 `gemini_3.1_flash_image_preview` |

## 2. 筛选判定

- **去重**：未在 `已反代项目记录表.md` / `反代落选-web.md` / `sessions/INDEX.md` 中出现 → 不重复
- **淘汰项逐项**：非纯聚合器 ❌ / 非 BYOK ❌ / 有免费额度（200c）✅ / 有生成能力 ✅ / 站点在线 ✅ / 有网页入口 ✅ / 非头部大众平台 ✅
- **结论：值得深入 → 已深入并完成实测**

## 3. 被动侦察发现

**前端框架**：Nuxt 3（SSR + hydration，`window.__NUXT_DATA__` 118KB payload），
静态资源域 `tripo-webapp-assets.tripo3d.ai/studio-prod/_nuxt/`，共 118 个 JS chunk / 6.17MB。

**域名清单**（资产面）：

| 域名 | 作用 | 证据 |
|--|--|--|
| `studio.tripo3d.ai` | Web 应用（CF challenge 保护） | Confirmed |
| `api.tripo3d.ai` | **主业务 API**（`/v2/studio/*`），无 CF 拦截 | Confirmed |
| `auth.tripo3d.ai` | Ory Kratos 身份服务（JWT issuer） | Confirmed |
| `msapi.tripo3d.ai` | 遥测（`/ms/web/api` 埋点，非业务） | Confirmed |
| `e-p.tripo3d.ai` | PostHog 分析 | Confirmed |
| `tripo-webapp-assets.tripo3d.ai` | CDN 静态资源 | Confirmed |
| `tripo-public.tripo3d.ai` | AWS 公网桶配置 | Confirmed |
| `staticassets.tripo3d.ai` | 头像/社区素材 | Confirmed |
| `tripo-data.rg1.data.tripo3d.com` | 产物 S3（CloudFront 签名 URL） | Confirmed |
| `platform.tripo3d.ai` | **官方开发者平台/文档** | Strong（bundle/营销页引用，未实测） |

**认证机制（三层）**：

1. `ory_kratos_session`（httpOnly cookie，`.tripo3d.ai`）——**30 天有效**（实测 `expires_at 2026-11-04`）
2. `Authorization: Bearer <ES256 JWT>`——**15 分钟有效**（实测 `exp-iat=900s`）
3. `csrf_token_<sha256>`（httpOnly cookie）+ `csrf_token` header

**必带自定义 header**（浏览器 CORS 预检暴露）：
`authorization`, `x-tripo-device-id`（= cookie `tripo_device_id`）, `x-tripo-start`（页面加载时间戳，性能埋点用）

**疑点/冲突**：
- Web 端 CF 严（curl 403 challenge）但 API 域完全裸奔 → **反代应直打 `api.tripo3d.ai`**
- `wm-billing/prices/list` 与 `billing/prices/list` 两条计费端点并存，前者带 `region: "US"` 本地化
- `progress` 端点 GET 返 405 → 需 POST 或已废弃
- 官方 OpenAPI 存在（`platform.tripo3d.ai/docs`）→ **2api 优先用官方 API key，私有端点仅作补充/降级方案**

## 4. 协议形态 — **Confirmed**

**传输**：REST / JSON，信封 `{code, message, data}`，`code:0` = 成功。
**无 SSE / 无 WebSocket** → 异步任务模型：**提交 task_id → 轮询**。

**端点表**（完整 115 条见 `artifacts/endpoint-table.txt`），核心：

| 方法 | 端点 | 作用 | 状态 |
|--|--|--|--|
| POST | `/v2/studio/image/gen_image_v2` | **图像生成** | **Confirmed 200 实测** |
| POST | `/v2/studio/image/get_image_asset` | 轮询任务/取产物 | **Confirmed 200 实测** |
| POST | `/v2/studio/image/available_templates` | 图像模板列表（9 个） | Confirmed 200 |
| POST | `/v2/studio/image/upscale` | 4K 放大 | 静态还原 |
| POST | `/v2/studio/image/gen_multiview` | 多视图生成 | 静态还原 |
| POST | `/v2/studio/image/image_assets` | 图像资产列表 | 静态还原 |
| POST | `/v2/studio/operation/text_to_model` | **文生 3D** | Confirmed 400 schema |
| POST | `/v2/studio/operation/image_to_model` | **图生 3D** | 静态还原 |
| POST | `/v2/studio/operation/multiview_to_model` | 多视图生 3D | 静态还原 |
| POST | `/v2/studio/operation/rigging_model` | 骨骼绑定 | 静态还原 |
| POST | `/v2/studio/operation/remesh` | 重拓扑 | 静态还原 |
| POST | `/v2/studio/operation/pbr_generate` | PBR 贴图 | 静态还原 |
| POST | `/v2/studio/operation/mesh_fill` | 补面 | 静态还原 |
| POST | `/v2/studio/operation/local_edit` | 局部编辑 | 静态还原 |
| POST | `/v2/studio/motion/generate` | 动捕/动作生成 | 静态还原 |
| POST | `/v2/studio/pro_refine/create` | Pro Refine | 静态还原 |
| POST | `/v2/studio/operation/quota` | **免费额度查询** | **Confirmed 200** |
| GET | `/v2/studio/studio/whoami` | 身份/Kratos session | **Confirmed 200** |
| GET | `/v2/studio/user/profile/payment` | **钱包/订阅/邀请码** | **Confirmed 200** |
| GET | `/v2/studio/marketing/export-limit` | 免费导出额度 | **Confirmed 200** |
| POST | `/v2/studio/wm-billing/prices/list` | 订阅价格表（18 币种） | Confirmed 200 |

**生成请求体（实测 schema）**：
```json
{"if_upload": true, "sketch_to_render": false, "t_pose": false,
 "prompt": "a red apple on white background",
 "model_version": "gemini_3.1_flash_image_preview",
 "image_number": 1, "aspect_ratio": "1:1"}
```
> 必填字段（服务端严格校验，逐个报 `field "X" is not set`）：`sketch_to_render`, `t_pose`, `prompt`, `model_version`
> 前端封装还会自动补 `if_upload`（`artifacts` 见 bundle `EO=e=>Y(...,{body:{if_upload:!0,sketch_to_render:!1,t_pose:!1,...e}})`）

**响应格式**：
```json
提交 → {"code":0,"data":{"task_id":"<uuid>","asset_id":"<uuid>","remaining_free_count":0}}
轮询 → {"code":0,"data":{"status":"running|success|failed","type":"generate_image",
        "input":{...},"output":{"data":[{"bucket":"tripo-data","key":"...","url":"<CloudFront签名URL>"}]}}}
```
**产物 URL 为 CloudFront 签名链接，含过期时间**（实测 `DateLessThan: 1791174639`，约 24h 有效），长期存储需落盘。

## 5. 门槛与破甲路线建议

**门槛**：
- CF Managed Challenge **仅挡 `studio.tripo3d.ai` 网页**，不挡 `api.tripo3d.ai` → **无 TLS 指纹问题**
- 无 Turnstile / 无 reCAPTCHA / 无滑块（登录为邮箱验证码，Ory Kratos `code` method）
- Ory Kratos session 30 天有效，JWT 15 分钟有效

**建议路线：🟢 A（纯 HTTP + cookie 复用）—— 已实测成功**

已验证可直连的最小请求头组合：
```
Authorization: Bearer <ES256 JWT>
x-tripo-device-id: <tripo_device_id cookie 值>
x-tripo-start: <任意浮点>
cookie: ory_kratos_session=...; csrf_token_93fef...=...
origin: https://studio.tripo3d.ai
referer: https://studio.tripo3d.ai/
```
> ⚠️ **JWT 15 分钟过期**：需实现续期机制 —— 有效期内用 `GET /v2/studio/studio/whoami` 可拿到
> Kratos `tokenized` 字段的续签 token（响应体已含 `tokenized` 字段，可用于换新 JWT）。
> 若续签失败则需人工重新登录（浏览器登录一次即可拿 30 天 session）。

**工程注意**：
- Python 直连必须用 `certifi`（macOS 系统 CA 会 SSL 报错，已踩坑）
- 校验错误（400）不消耗额度，可安全用于 schema 探测 —— 这是本次还原 body schema 的关键手法
- 官方 OpenAPI（`platform.tripo3d.ai`）有 API key 体系，**长期应优先用官方**，私有端点作为无 key 时的降级

**需要人工介入的点**：JWT 续签失败时需人工浏览器登录一次（30 天一次）。

## 5.5 3D 生成专项（第三轮）— **✅ 实测跑通，下载到 GLB**

> ⚠️ **本节推翻第二轮的「3D 付费墙结题」结论。**
> 第二轮我用**猜的 body 打 `text_to_model`**，拿到 `403 6102` 就判了死刑 —— 这是错的。
> 真相：`text_to_model`（文生 3D）确实会员专属，但 **`image_to_model`（图生 3D）不设防**，
> 而 free 账号的 UI **只暴露图生 3D**（没有 prompt 输入框，只有上传框）。
> **没打开 UI 实际点一次，就下不了这个结论。**

### 实测成功记录（Confirmed，全链路）

| 项 | 值 |
|--|--|
| 输入 | 上传 PNG（1.1MB，红色苹果） |
| 提交 | `POST /v2/studio/operation/image_to_model` → **200** |
| operator_id | `ededce5f-5409-4e08-9d06-b5cf82e657f3` |
| project_id | `0364783a-ca81-4c72-8200-052b0bc5b624` |
| 扣费 | **55 credits**（180 → 125），UI 显示「生成 55」 |
| 耗时 | **约 5.5 分钟**（`left_time` 倒计时 373s → 0） |
| 产物 | `tripo_pbr_model_<operator_id>_meshopt.glb` |
| **下载** | **15,805,980 字节 有效 GLB** → `artifacts/apple_3d.glb` |

**GLB 校验（已解析 header + JSON chunk）**：

```
magic=glTF  version=2  declared=15805980  actual=15805980
asset: {version: '2.0', generator: 'gltfpack 1.2'}
meshes: 1  materials: 1  images: 3  textures: 3
POSITION vertices = 1,010,879     indices = 5,963,142  →  triangles = 1,987,714
attributes: POSITION / NORMAL / TEXCOORD_0
```
> 198 万面说明 `face_limit:2000000` 被真实应用（不是 UI 摆设），
> 且 `texture:true + pbr:true` 产出了 3 张贴图 + 材质 → **PBR 贴图烘焙成功**。

### 上传流程（3 步，Confirmed）

1. `POST /v2/studio/storage/temporary_token` `{client:"aws",format:"png"}` → 拿 presigned S3 PUT 直传
2. `POST /v2/studio/audit/image` `{image:{bucket,key}}` → 返回 `image_audit_result:"pass"`
   **⚠️ 该字段是 submit body 的必填项**，必须先过审
3. `POST /v2/studio/operation/image_to_model`（body 见下）

### 完整 submit body（UI 原文，Confirmed）

```json
{"face_limit":2000000,"quad":false,"visibility":"public",
 "model_version":"v3.1-20260211","generate_parts":false,"smart_poly":false,
 "texture":true,"delight":false,"pbr":true,
 "texture_alignment":"original_image","texture_quality":"detailed",
 "enable_image_autofix":false,"geometry_quality":"detailed",
 "image":{"bucket":"tripo-data","image_audit_result":"pass","image_source":"upload",
          "key":"tripo-studio/20261005/<uuid>/input.png"}}
```

> ⚠️ **注意与第二轮猜的 body 有 3 处实质差异**：
> - `texture_alignment` 真实值是 **`"original_image"`**，不是 `"Image"`
> - `geometry_quality` / `texture_quality` 小写成 **`"detailed"`**，不是 `"Detailed"`
> - 多了 `delight:false`、`visibility:"public"`，且 `face_limit` 用满 2000000
>
> → 第二轮我猜的 `"Detailed"` 大小写错误，若当时会员门放开也会失败。
> **结论：body 必须来自真实抓包，不能靠猜。**

### 轮询协议（Confirmed）

```
POST /v2/studio/progress   {"ids":[operator_id]}
→ {"status":"running","progress":28,"left_time":235,"point_cloud_model_url":...}
→ ...
→ {"status":"success","progress":100,...}
再 GET /v2/studio/project/detail/v3/<project_id>?locale=zh-CN  →  data.model_url
```

**两个实测坑**：
- **99% 会卡 90–120 秒**（贴图烘焙阶段），此时 `left_time` 已接近 0 但 `status` 仍是 `running`
  → 轮询器**不能以 `left_time==0` 或 `progress==100` 判定完成，必须等 `status=="success"`**
- 无 SSE / 无 WebSocket，纯轮询（与图像生成同构）

### 仍被门禁的 3D 能力（修正后）

| 能力 | 端点 | 状态 | 证据 |
|--|--|--|--|
| **图生 3D** | `image_to_model` | ✅ **可用 55c** | 实测 success + GLB |
| 文生 3D | `text_to_model` | ❌ 403 6102 | UI **无 prompt 输入框**，全页仅 2 个 file input |
| 多视图 | `multiview_to_model` | ❌ 升级弹窗 | 实测弹「专业版 ¥9 / 旗舰版 ¥315」 |
| 分部件 | `generate_parts` | ❌ | UI 标「订阅专享」 |
| 8K 贴图 | — | ❌ | UI 标「试用 ×1」 |
| 智能网格 P2 | `smart_poly` | ❌ 死额度 | quota 显示 2，但 403 6102 拦在扣减前 |
| 智能 UV | `uv_edit/` | ❌ | quota 显示 2，未找到 free 入口 |

### UI 自动化要点（可复现）
- **生成按钮必须用真实鼠标事件点击**：`b.click()` 会被前端静默吞掉（无请求发出），
  必须 `Input.dispatchMouseEvent` mousePressed/mouseReleased 打在按钮 rect 中心
- **按钮文本要区分**：「生成 55」是主按钮；「生成多视图」是付费入口。
  正则 `^生成\s*\n?\s*\d+$` 精确命中主按钮（点错会弹升级弹窗）
- 上传用 `DOM.setFileInputFiles` 打 196×196 的 file input；
  判据 = 「生成多视图」按钮出现 = 审核通过

## 5.6 导出通道专项：免费拿到的 GLB vs 会员导出的 GLB

用户反馈"官网下载要开会员"—— **核实成立**，但存在两条免费通道，且有重要发现。

### 三条导出通道实测

| 通道 | 方法/端点 | 必需字段 | 结果 |
|--|--|--|--|
| **A. 运行时产物**（免费） | `GET /project/detail/v3/<pid>?locale=zh-CN` | — | ✅ **200** → `data.model_url` |
| **B. 带名下载**（免费） | `POST /operation/download_with_name` | `{file_name, operator_id}` | ✅ **200** → `data.model_url` |
| **C. 正式导出**（会员） | `POST /operation/export` | `{project_id, name, model_version, format, packaging}` | ❌ **403 6102** |
| **C'. 轻量导出**（会员） | `POST /operation/export-lite` | `{operator_id, project_id, format, is_owner}` | ❌ **403 6102** |

> 6 种格式（`glb/usd/fbx/obj/stl/3mf`）× 通道 C 全部 403 —— **会员门在格式转换之前**，
> 与选什么格式无关。
>
> `export-limit` 额度 `{total_count:15, used_count:0}` 我原以为是"15 次免费导出"，
> **实测证伪**：free 账号走不到那一步，额度是会员的。

### ⚠️ 关键发现：`file_name` 扩展名被完全忽略

对通道 B 用 6 种扩展名各调一次：

```
a.glb   -> ..._meshopt.glb  15,805,980 B  magic=b'glTF'
a.stl   -> ..._meshopt.glb  15,805,980 B  magic=b'glTF'
a.obj   -> ..._meshopt.glb  15,805,980 B  magic=b'glTF'
a.fbx   -> ..._meshopt.glb  15,805,980 B  magic=b'glTF'
a.usdz  -> ..._meshopt.glb  15,805,980 B  magic=b'glTF'
a.3mf   -> ..._meshopt.glb  15,805,980 B  magic=b'glTF'
```

**6 次返回完全相同的 S3 key、相同的字节数、相同的 `glTF` 魔数。**
→ `file_name` 只是**显示名**，不是格式请求。**不存在任何免费格式转换路径。**
（此前我误以为「200 就说明免费导出通了」，验证字节后才看穿 —— 记录此教训）

### Q1：下载的 GLB 和开会员下载的是同一个吗？

**几何与贴图：是同一个源产物。格式能力：不是。**

| 维度 | 免费拿到的 meshopt GLB | 会员导出 |
|--|--|--|
| 顶点/法线/UV | **同一份数据**（1,010,879 顶点 / 1,987,714 面） | 同一份数据重新序列化 |
| PBR 贴图 | **3 张已烘焙**，材质完整 | 同上 |
| 质量 | **不是缩水版** —— 面数顶满 `face_limit:2000000` | 同 |
| 格式转换 | ❌ 只能 GLB | USD/FBX/OBJ/STL/3MF |
| 打包 | ❌ 单文件 | `embedded` / `zip` |
| 贴图分辨率切换 | ❌ 固定 2K 档 | 512/1K/2K/4K/8K（8K 需订阅） |

> **一句话**：`model_url` 就是模型在 S3 上的原生存储形态，会员导出是把它**重新序列化**成
> 别的格式。**数据本身一样，容器和格式能力不一样。**

### Q2：export-lite 是 STL/OBJ 等格式下载，绕不过吗？

**对，绕不过。** 它和 `export` 一样是会员功能（都是 403 6102）。
`export-lite` 的区别只是"lite"= 少一步交互（直接给文件，不进导出任务队列），
**不是免费通道**。schema 已完整探明：`{operator_id, project_id, format, is_owner}`。

**但有个免费替代路径**（不涉及 Tripo 会员）：
```bash
# gltf-transform（本地处理，与 Tripo 无关）
npm i -g @gltf-transform/cli
gltf-transform optimize apple_3d.glb apple_opt.glb          # meshopt -> Draco 或解压
gltf-transform copy apple_3d.glb apple_plain.glb            # 解压掉 meshopt
# 转 STL/OBJ 用 assimp：
brew install assimp
assimp export apple_3d.glb apple.obj
assimp export apple_3d.glb apple.stl
```
→ 会员买的是"**服务端帮你转**"，不是"转换能力本身"。本地转换完全免费且格式更全。

### Q3：meshopt 影响哪些软件

**文件特征**：
```
extensionsRequired: ['KHR_mesh_quantization', 'EXT_meshopt_compression']
4 个 bufferView 全部走 EXT_meshopt_compression
  POSITION  → 原始 float32 (5126)
  NORMAL    → 量化 int8  (5120, normalized)  ← 量化了
  TEXCOORD  → 原始 float32 (5126)
  INDICES   → uint32 (5125, 未量化)
体积：15.8 MB 压缩  vs  ~56 MB 原始浮点估算  = 约 3.6×
```
> 注意：`KHR_mesh_quantization` 主要影响法线（int8），**位置坐标仍是 float32**，
> 所以几何精度没问题；省体积的是 meshopt 的熵编码 + 索引重排。

**✅ 不受影响（直接能开）**
- Blender **≥ 3.0**（内置 meshopt）
- **three.js**（`GLTFLoader` + `MeshoptDecoder`，约 30 行接入；本站自己的 bundle 就在用 —— 证据：`new ti().setMeshoptDecoder(fi)`）
- Unity（GLTFast + meshopt）、Unreal Engine 5、Babylon.js、`<model-viewer>`
- `gltfpack` / `gltf-transform` CLI、Khronos glTF-Validator

**❌ 受影响**
- **Blender < 3.0** —— 可能加载失败或丢网格
- **3D 打印切片软件（PrusaSlicer / Cura）** —— **不支持 glTF 扩展，也不吃 meshopt**，
  必须先转 STL/OBJ。**这是会员墙真正会咬到你的地方**
- 朴素 glTF 解析器（部分老 three.js < r120、部分网页查看器）—— ⚠️ **危险失效模式：
  忽略 `extensionsRequired` 会静默读到空 buffer，表现为"模型加载了但是空的/只有一点"**
- 不支持扩展的 DCC / 转换工具链

**结论**：如果你的下游是 **Web / Blender / 游戏引擎** → 免费 GLB 直接够用，无影响。
如果下游是 **3D 打印** 或 **需要 FBX/OBJ 交换** → 要么本地 `assimp`/`gltf-transform` 转，
要么开会员（省事但没必要花钱）。

## 6. 待验证 / 未知

| 编号 | 假设/未知 | 需要什么验证 | 是否需授权 |
|--|--|--|--|
| H1 | ✅ 已解决 | 3D 图生可用 55c | — |
| H2 | JWT 续签机制可用 | `auth.tripo3d.ai/sessions/whoami?tokenize_as=default_jwt` + `X-Session-Token` | 部分（需原始 session token） |
| H3 | ✅ 已解决 | body schema 已从真实 UI 抓包 | — |
| H4 | 官方 OpenAPI 是否有独立免费额度 | 读 `platform.tripo3d.ai/docs` | 否（公开页） |
| H5 | 多图批量扣费是否线性 | `image_number:4` | 是（消耗额度） |
| H6 | 团队工作区能否解锁 smart_mesh_v2 / text_to_model | 需创建团队 | 是 |
| H7 | 文生 3D 是否有非 UI 入口 | 试 `text_to_model` + `visibility`/`style` 等变体 | 部分（400 探测可，200 需会员） |
| H8 | 其他 3D 后缀导出（FBX/OBJ/USD）免费额度内是否可用 | `marketing/export-limit` 15 次未用 | 是（消耗额度） |

## 7. 证据链

| 结论 | 证据（原文/引用） | 来源 | 置信度 |
|--|--|--|--|
| 认证 = ES256 JWT | `{"alg":"ES256","kid":"X9ZtY9k9L4NEA57L8bWLui9AcOz-HjpvxPcSy2RxCrs"}` + payload `{"iss":"https://auth.tripo3d.ai","aud":["tripo"],"exp":...,"iat":...}` exp-iat=900 | CDP 抓包 `authorization` 头 | **Confirmed** |
| 身份 = Ory Kratos 邮箱登录 | `whoami` → `authentication_methods:[{"method":"code"}]`、`identity.traits.email`、`expires_at 2026-11-04` | `GET /v2/studio/studio/whoami` 200 | **Confirmed** |
| 必带自定义 header | CORS 预检 `access-control-request-headers: authorization,x-tripo-device-id,x-tripo-start` | HAR 响应头 | **Confirmed** |
| 生成端点可用 | `{"code":0,"data":{"task_id":"920a630b-09e3-478a-816e-7edd824b13c8","asset_id":"...","remaining_free_count":0}}` | `POST /v2/studio/image/gen_image_v2` 200 | **Confirmed** |
| 产物可取 | `"status":"success"`, `"url":"https://tripo-data.rg1.data.tripo3d.com/generate-image/20261005/<uuid>/image.png?Key-Pair-Id=...&Policy=...&Signature=..."` | `artifacts/resp-gen_image_v2.json` | **Confirmed** |
| 免费额度 200c | `{"wallet":{"total_credit":200,"expiring_credit":200,"expiring_date":"2026-11-01"}}` | `GET /v2/studio/user/profile/payment` 200 | **Confirmed** |
| 单价 10c/图 | 生成后复测 `total_credit:190`；二次生成后再验仍 200 | 前后 wallet 对比 | **Confirmed** |
| 3D 图生可用 | `POST /operation/image_to_model` 200 → `progress` 100% success → 下载 `tripo_pbr_model_..._meshopt.glb` 15,805,980 B | `artifacts/apple_3d.glb` + `req-3d-confirmed.json` | **Confirmed** |
| GLB 有效非占位 | `magic=glTF version=2 declared=15805980 actual=15805980; generator=gltfpack 1.2; 1,010,879 verts / 1,987,714 tris; 3 textures` | 本地 header+JSON chunk 解析 | **Confirmed** |
| 3D 单价 55c | 提交前 180 → 提交后 125 | `GET /user/profile/payment` 前后对比 | **Confirmed** |
| face_limit 真实生效 | 提交 `face_limit:2000000` → 产物 1,987,714 tris | body × GLB 解析交叉验证 | **Confirmed** |
| PBR 贴图真实烘焙 | 提交 `pbr:true,texture:true` → 产物 `images:3, textures:3, materials:1` | 同上 | **Confirmed** |
| 99% 会卡 90-120s | 连续 20 次轮询 `progress:99, left_time:1` 而 `status:running` | 轮询日志 | **Confirmed** |
| 文生 3D 门禁 | `403 6102` + UI 全页仅 2 个 `input[type=file]`、0 个文本框 | 400/403 探测 + DOM 遍历 | **Confirmed** |
| 多视图付费 | 点击后弹「专业版 ¥9 / 旗舰版 ¥315」 | UI 实测 | **Confirmed** |
| 3D 端点 schema 合法但会员不足 | 7 个版本 + smart_poly 开关全试 → `{"code":6102,"message":"Insufficient membership"}` | `POST operation/text_to_model` 403 | **Confirmed** |
| 显示名不是 API 版本值 | `400 {"code":1004,"message":"invalid modelVersion: Tripo H3.1"}` | `POST operation/text_to_model` 400 | **Confirmed** |
| smart_mesh_v2 额度不可达 | `quota:{smart_mesh_v2:{total:2,remaining:2,is_valid:true}}` 但提交 `403 6102` | 两端点交叉 | **Confirmed** |
| `image-prompt-model` 已下线 | `410 {"code":2040,"message":"This endpoint is no longer available"}` | `POST operation/image-prompt-model` 410 | **Confirmed** |
| `symmetry_check` 免费 | `200 {"code":0,"data":{"symmetry":true}}` | 实测 | **Confirmed** |
| 8 个图像模型 | `FLUX_1_dev=\`flux.1_dev\``, `FLUX_1_pro=\`flux.1_kontext_pro\``, `GPT_image_1_5=\`gpt_image_1.5\``, `GPT_image_2=\`gpt_image_2\``, `GPT_image_2_5=\`gpt_image_2.5_sunburst\``, `Midjourney=\`midjourney\``, `NanoBanana=\`gemini_2.5_flash_image_preview\``, `NanoBanana_2=\`gemini_3.1_flash_image_preview\``, `NanoBanana_pro=\`gemini_3_pro_image_preview\`` | bundle regex 扫描 | **Confirmed**（`gemini_3.1` 实测跑通） |
| 13 个 3D 模型版本 | `Nexus-v1.0-20260214`, `Nexus-v2.0-20260801`, `Tripo H2.5`, `Tripo H3.0`, `Tripo H3.1`, `Tripo P1.0`, `Tripo P2.0`, `v1.0-20240301`, `v1.0-20250506`, `v2.0-20250506`, `v2.0-20260430`, `v2.5-20250123`, `v2.5-20260210`, `v3.0-20250812`, `v3.0-20260909`, `v3.1-20260211` | bundle enum `e.xxx=\`...\`` | **Confirmed** |
| 115 个端点 | 见 `artifacts/endpoint-table.txt` | bundle rg 扫描 | **Confirmed**（10 余个已实测 200） |
| CF 不拦 API 域 | curl 网页 → `403 cf-mitigated: challenge`；curl `api.tripo3d.ai` → `200` | 双路径对比 | **Confirmed** |
| 邀请奖励规则 | `invitation:{code:"5B9EWY", get_member_condition:30, register_generation_limit:50}` | `payment` 端点 | **Confirmed**（数值来自后端非营销页） |

## 8. 下一步

**范围**：图像 + 3D 图生均已实测跑通，协议/schema/成本全部确认，**无未解技术阻塞**。

**可立即落地 `tripo2api` 网关**：
- `GET /v1/models` ← 8 图像模型（`flux.1_dev`/`flux.1_kontext_pro`/`gpt_image_1.5`/
  `gpt_image_2`/`gpt_image_2.5_sunburst`/`midjourney`/`gemini_2.5_flash_image_preview`/
  `gemini_3.1_flash_image_preview`/`gemini_3_pro_image_preview`）
  + 7 个 3D 版本（`v1.0-20250506`…`v3.1-20260211`, `Nexus-v1.0-20260214`, `Nexus-v2.0-20260801`）
  + `POST operation/quota` 动态余额
- `POST /v1/images/generations` ← `gen_image_v2`（10c/张）→ 轮询 `get_image_asset`
- `POST /v1/3d/generate` ← `storage/temporary_token` → S3 PUT → `audit/image`
  → `image_to_model`（55c）→ 轮询 `progress` 至 `status=="success"` → `project/detail/v3` 取 GLB
- **共用轮询器**：无 SSE，统一 `status==success` 判定（`progress`/`left_time` 不可信，99% 会卡 90-120s）
- **成本对照**：图像 10c/张 vs 3D 55c/个 → 200c 约 20 张图 或 3 个 3D 模型

**凭证**：Kratos session（30d）+ JWT 缓存（15m）；⚠️ **续签未打通** ——
`auth.tripo3d.ai/sessions/whoami?tokenize_as=default_jwt` + `X-Session-Token`，
需原始 session token（非 base64 cookie 值），需在浏览器内 `fetch` 才能取到。
**最简解**：直接在 Playwright/CDP 浏览器上下文里调 API（复用登录态，绕过 JWT 生命周期问题）

**止损点**：若官方 API key 可得 → 立即改走官方（`platform.tripo3d.ai`，无 JWT 过期问题）。

---

## 9. 方法论复盘（3 轮探测的关键教训）

1. **猜 body 打不通 ≠ 有付费墙。** 第二轮我用推测的 body 打 `text_to_model` 得 403，
   就判了"3D 放弃"。真实情况是 body 大小写错了（`Detailed` vs `detailed`），
   且我探错了端点 —— free UI 根本不提供 `text_to_model`。
   **有 UI 的产品，必须先驱动一次真实 UI 拿到运行时请求，不能只靠静态 bundle 推断。**

2. **同平台的端点权限可以完全不同。** `text_to_model` 403 不代表 `image_to_model` 也 403。
   逐端点验证，不能一票否决。

3. **前端按钮可能"存在但不可用"。** 生成按钮一直渲染在那里且 `disabled=false`，
   但因为没有输入内容，`.click()` 被静默吞掉，零请求发出。
   **UI 自动化必须用 `Input.dispatchMouseEvent` 打真实坐标，不能用 DOM `.click()`。**

4. **轮询判定不能信进度字段。** `progress=100` / `left_time=0` 时 `status` 仍可能是 `running`（贴图烘焙），
   必须等 `status=="success"`。

5. **额度显示 ≠ 额度可达。** `quota` 报 `smart_mesh_v2: remaining 2`，
   但会员门在扣减前拦截 → 死额度。**判据是能否实际提交，不是能否读到数字。**

#!/usr/bin/env python3
"""
tripo2api 本地 Console 服务
- GET  /                    -> console.html
- GET  /v1/studio/*         -> 反代 api.tripo3d.ai 同路径（浏览器同源，避免 CORS）
- POST /v1/studio/*         -> 同上
- GET  /v1/accounts         -> 账号池列表
- POST /v1/accounts/refresh-credits -> 逐个刷新额度
- POST /v1/accounts/capture -> 从 9366 Chrome 抓取当前登录账号（无需密码）
- POST /v1/accounts/remove  -> 移除账号
- GET  /v1/accounts/export  -> 导出 JSON

账号池存储：accounts.json（与 config.json 同格式的凭证数组）
单个账号格式：
  {
    "label": "主号",
    "email": "...",
    "jwt": "<ES256 JWT，15 分钟有效>",
    "device_id": "...",
    "cookie": "...",
    "csrf": "...",
    "sub": "<JWT sub>",
    "added_at": 0
  }
"""
import base64, json, os, re, ssl, sys, time, urllib.request, urllib.error
from urllib.parse import urlparse, parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import keepalive as ka_mod

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(ROOT, "config.json")
POOL_PATH = os.path.join(ROOT, "accounts.json")
UPSTREAM = "https://api.tripo3d.ai"
PORT = int(os.environ.get("TRIPO_PORT", "4878"))
CDP_PORT = int(os.environ.get("TRIPO_CDP_PORT", "9366"))

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    SSL_CTX = None


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, ensure_ascii=False)


def load_cfg():
    return load_json(CFG_PATH, {})


def load_pool():
    """账号池（SQLite）。首次运行且表为空时，从 config.json 兜底导入一次。"""
    rows = db.list_accounts(with_secrets=True)
    if rows:
        return rows
    cfg = load_cfg()
    if cfg.get("jwt") or cfg.get("cookie"):
        acc = {"sub": "email:" + (cfg.get("email") or "bootstrap"),
               "label": "主号 (config.json 导入)",
               "email": cfg.get("email") or "",
               "jwt": cfg.get("jwt", ""), "device_id": cfg.get("device_id", ""),
               "cookie": cfg.get("cookie", ""), "csrf": cfg.get("csrf", "")}
        db.upsert_account(acc)
        return [db.get_account(acc["sub"])]
    return []


def save_pool(pool):
    for a in pool:
        if isinstance(a, dict) and (a.get("sub") or a.get("email")):
            db.upsert_account(a)


def call_upstream(path, method, body, acct):
    """用指定账号的凭证请求上游 /v2/studio/* 路径。"""
    headers = {
        "accept": "application/json",
        "origin": "https://studio.tripo3d.ai",
        "referer": "https://studio.tripo3d.ai/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
    }
    if acct.get("jwt"):
        headers["authorization"] = "Bearer " + acct["jwt"]
    if acct.get("device_id"):
        headers["x-tripo-device-id"] = acct["device_id"]
    headers["x-tripo-start"] = "%.1f" % (time.perf_counter() * 1000)
    if acct.get("cookie"):
        headers["cookie"] = acct["cookie"]
    if acct.get("csrf"):
        headers["csrf_token"] = acct["csrf"]
    if body:
        headers["content-type"] = "application/json"
    url = UPSTREAM + path
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=90, context=SSL_CTX)
        return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"error": {"message": "upstream error: %s" % e}}).encode()


def refresh_jwt(acct):
    """用 whoami?tokenizeAs=default_jwt 换新 JWT（15 分钟过期）。"""
    code, data = call_upstream("/v2/studio/studio/whoami?tokenizeAs=default_jwt", "GET", None, acct)
    if code != 200:
        return None
    try:
        d = json.loads(data)
    except Exception:
        return None
    return d.get("tokenized")


def probe_account(acct):
    """拉取该账号的 email / credits / 过期日 / quota。"""
    out = dict(acct)
    out.pop("jwt", None)
    out.pop("csrf", None)
    out["jwt_tail"] = (acct.get("jwt") or "")[-10:]

    # JWT 过期检查
    jwt = acct.get("jwt") or ""
    out["jwt_expired"] = True
    if jwt.count(".") == 2:
        import base64
        try:
            p = jwt.split(".")[1]; p += "=" * (-len(p) % 4)
            pl = json.loads(base64.urlsafe_b64decode(p).decode())
            out["jwt_exp"] = pl.get("exp")
            out["jwt_expired"] = pl.get("exp", 0) < time.time()
            out["sub"] = pl.get("sub", out.get("sub", ""))
        except Exception:
            pass

    code, data = call_upstream("/v2/studio/user/profile/payment", "GET", None, acct)
    out["ok"] = code == 200
    out["status"] = code
    if code == 200:
        try:
            d = json.loads(data).get("data", {})
            w = d.get("wallet", {})
            m = d.get("member", {})
            out["email"] = d.get("email") or out.get("email") or ""
            out["plan"] = m.get("type", "")
            out["free_trial"] = m.get("free_trial", False)
            out["credits"] = w.get("total_credit")
            out["expiring_credit"] = w.get("expiring_credit")
            out["expiring_date"] = w.get("expiring_date", "")
            out["member_end"] = m.get("end_time", "")
            out["invite_code"] = (d.get("invitation") or {}).get("code", "")
            out["has_glb_export"] = None
        except Exception as e:
            out["error"] = str(e)
    else:
        out["error"] = data[:160].decode("utf-8", "replace")
        # 401 -> JWT 过期，尝试自动续签
        if code == 401 and not out["jwt_expired"]:
            pass

    # 免费额度
    code2, data2 = call_upstream("/v2/studio/operation/quota", "POST", b"{}", acct)
    if code2 == 200:
        try:
            q = json.loads(data2).get("data", {}).get("quota", {})
            g = q.get("generation", {})
            uv = (q.get("uv_edit") or {}).get("generate", {})
            out["quota_smart_mesh"] = (g.get("smart_mesh_v2") or {}).get("remaining")
            out["quota_uv_edit"] = uv.get("remaining")
        except Exception:
            pass

    # 资产数
    code3, data3 = call_upstream(
        "/v2/studio/assets/v2?asset_type=mine&locale=zh-CN&offset=0&size=1&type=all", "GET", None, acct)
    if code3 == 200:
        try:
            dd = json.loads(data3).get("data", {})
            out["assets"] = dd.get("total")
            out["assets_max"] = dd.get("max_assets")
        except Exception:
            pass
    return out


def probe_assets(acct, size=50):
    """拉某账号的云端资产列表（真实上限来自服务端 max_assets）。"""
    code, data = call_upstream(
        "/v2/studio/assets/v2?asset_type=mine&locale=zh-CN&offset=0&size=%d&type=all" % size,
        "GET", None, acct)
    if code != 200:
        return {"ok": False, "error": data[:160].decode("utf-8", "replace"), "projects": [],
                "total": 0, "max_assets": None}
    try:
        d = json.loads(data).get("data", {}) or {}
    except Exception:
        return {"ok": False, "error": "parse fail", "projects": [], "total": 0, "max_assets": None}
    out = []
    for p in (d.get("projects") or []):
        if not isinstance(p, dict):
            continue
        oper = p.get("operator") or {}
        if not isinstance(oper, dict):
            oper = {}
        itm = oper.get("image_to_model") or oper.get("text_to_model") or {}
        if not isinstance(itm, dict):
            itm = {}
        url = p.get("model_url") or ""
        bucket, key = _s3_parts(url)
        # 源图（生图→3D 链路回溯）
        src_key = None
        ii = itm.get("image") or {}
        if isinstance(ii, dict):
            src_key = ii.get("key")
        elif isinstance(ii, str):
            src_key = ii
        out.append({
            "id": p.get("id"),
            "name": p.get("project_name") or "(未命名)",
            "type": p.get("type") or "generate",
            "visibility": p.get("visibility"),
            "status": oper.get("status") or "unknown",
            "operator_id": oper.get("operator_id"),
            "model_version": itm.get("model_version") or oper.get("model_version"),
            "face_limit": itm.get("face_limit"),
            "quad": itm.get("quad"),
            "is_pbr": oper.get("is_pbr"),
            "is_textured": oper.get("is_textured"),
            "is_rigged": oper.get("is_rigged"),
            "is_segmented": oper.get("is_segmented"),
            "is_ultra_textured": oper.get("is_ultra_textured"),
            "is_hd_textured": oper.get("is_hd_textured"),
            "is_nexus_mesh": oper.get("is_nexus_mesh"),
            "category": p.get("category"),
            "use_case": p.get("use_case"),
            "cover_image": (p.get("cover_image") or [None])[0],
            "cover_key": (((p.get("cover_image_object") or [{}])[0]) or {}).get("key"),
            "model_url": url or None,
            "s3_bucket": bucket, "s3_key": key,
            "src_image": src_key,
            "created_at": p.get("created_at") or oper.get("created_at"),
            "updated_at": p.get("updated_at") or oper.get("updated_at"),
        })
    return {"ok": True, "total": d.get("total"), "max_assets": d.get("max_assets"),
            "projects": out}


def sign_expiry(url):
    """从 CloudFront 签名 URL 的 Policy 里解析过期时间戳（秒）。解析失败返回 None。"""
    if not url:
        return None
    try:
        import urllib.parse as up, base64, re
        pol = up.parse_qs(up.urlparse(url).query).get("Policy", [""])[0]
        if not pol:
            return None
        b = pol.replace("-", "+").replace("_", "/")
        b += "=" * ((4 - len(b) % 4) % 4)
        s = base64.b64decode(b).decode("latin-1")
        m = re.search(r"\{.*?\}(?=\s*\x00|\s*[^\x20-\x7e]|$)", s)
        if not m:
            return None
        return int(json.loads(m.group(0))["Statement"][0]["Condition"]["DateLessThan"]["AWS:EpochTime"])
    except Exception:
        return None


def classify_storage(row):
    """判定该产物的存储状态：signed_valid / signed_soon / key_only / no_key。"""
    url = row.get("url")
    key = row.get("s3_key")
    exp = sign_expiry(url) if url else None
    now = time.time()
    if not key and not url:
        return {"state": "no_key", "expiry": None, "left_h": None}
    if exp and exp > now + 900:          # 还有 >15 分钟
        return {"state": "signed_valid", "expiry": int(exp), "left_h": round((exp - now) / 3600, 1)}
    if exp:
        return {"state": "signed_soon", "expiry": int(exp), "left_h": round((exp - now) / 3600, 1)}
    return {"state": "key_only", "expiry": None, "left_h": None}


def fetch_fresh_url(acct, kind, ref_id):
    """按 s3_key 换一个新的签名 URL。

    已验证：`get_image_asset` / `project/detail/v3` 每次调用都会重新签名，
    所以不需要专门的"换签名"端点——现有查询端点本身就是签名服务。
    """
    code, data = call_upstream("/v2/studio/image/get_image_asset", "POST",
                               json.dumps({"asset_id": ref_id}).encode(), acct)
    if code == 200:
        try:
            d = json.loads(data).get("data") or {}
            items = ((d.get("output") or {}).get("data")) or []
            if items and items[0].get("url"):
                return items[0]["url"], None
        except Exception:
            pass
        return None, "asset 无产物 url（status=%s）" % d.get("status")
    return None, "get_image_asset HTTP %d" % code


def fetch_fresh_url_3d(acct, project_id):
    code, data = call_upstream("/v2/studio/project/detail/v3/%s?locale=zh-CN" % project_id,
                               "GET", None, acct)
    if code == 200:
        try:
            d = json.loads(data).get("data") or {}
            p = d.get("project") or d
            if p.get("model_url"):
                return p["model_url"], None
            return None, "project 无 model_url（status=%s）" % (p.get("operator") or {}).get("status")
        except Exception:
            pass
    return None, "project detail HTTP %d" % code


def _s3_parts(url):
    """从 CloudFront 签名 URL 里抠出 bucket/key。"""
    if not url or not isinstance(url, str):
        return None, None
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path.lstrip("/")
        if not path:
            return None, None
        seg = path.split("/")
        bucket = "tripo-data"
        if len(seg) >= 3 and seg[0].startswith("tripo-"):
            bucket = seg[0]
            return bucket, path[len(seg[0]) + 1:]
        return bucket, path
    except Exception:
        return None, None


def record_artifact(path, method, raw, code, data, acct, dt):
    """把生成请求/轮询的产物落库（幂等）。只关心几个关键端点。"""
    if method != "POST" or code not in (200, 201):
        return
    try:
        body = json.loads(data)
    except Exception:
        return
    if not isinstance(body, dict) or body.get("code") != 0:
        return
    d = body.get("data") or {}
    sub, email = acct.get("sub"), acct.get("email")
    p = urlparse_path(path)

    # ---- 生图提交 ----
    if p.endswith("/image/gen_image_v2"):
        aid = d.get("asset_id") or d.get("task_id")
        inb = {}
        try:
            inb = json.loads(raw or b"{}")
        except Exception:
            pass
        db.upsert_image({
            "asset_id": aid, "task_id": d.get("task_id"), "account_sub": sub, "account_email": email,
            "model_version": inb.get("model_version"), "prompt": inb.get("prompt"),
            "aspect_ratio": inb.get("aspect_ratio"), "resolution": inb.get("resolution") or "1K",
            "amount": inb.get("image_number") or 1, "template_id": inb.get("template_id"),
            "t_pose": inb.get("t_pose"), "sketch": inb.get("sketch_to_render"),
            "status": "running", "credits": 10 * (inb.get("image_number") or 1),
            "url": None,
        })
        db.log_event("image", "submitted", ref_id=aid, account_sub=sub, status_code=code,
                     credits=10 * (inb.get("image_number") or 1), duration_ms=dt,
                     detail={"model": inb.get("model_version"), "prompt": (inb.get("prompt") or "")[:80]})
        sys.stderr.write("[db] image submitted %s\n" % aid)

    # ---- 生图轮询：成功时补 url ----
    elif p.endswith("/image/get_image_asset"):
        aid = d.get("asset_id")
        st = d.get("status")
        items = ((d.get("output") or {}).get("data")) or []
        url = items[0].get("url") if items else None
        bucket, key = _s3_parts(url)
        if st == "success" and url:
            db.upsert_image({"asset_id": aid, "status": "success", "url": url,
                             "s3_bucket": bucket, "s3_key": key,
                             "audit_result": items[0].get("image_audit_result")})
            db.log_event("image", "success", ref_id=aid, account_sub=sub, status_code=code, duration_ms=dt)
            sys.stderr.write("[db] image success %s -> %s\n" % (aid, key))
        elif st == "failed":
            db.upsert_image({"asset_id": aid, "status": "failed"})
            db.log_event("image", "failed", ref_id=aid, account_sub=sub, status_code=code)

    # ---- 3D 提交 ----
    elif p.endswith("/operation/image_to_model") or p.endswith("/operation/text_to_model") \
            or p.endswith("/operation/multiview_to_model"):
        op = d.get("operator_id") or d.get("task_id") or d.get("id")
        if not op:
            return
        inb = {}
        try:
            inb = json.loads(raw or b"{}")
        except Exception:
            pass
        src = p.rsplit("/", 1)[-1]
        db.upsert_model3d({
            "operator_id": op, "project_id": d.get("project_id") or d.get("asset_id"),
            "account_sub": sub, "account_email": email, "name": None,
            "model_version": inb.get("model_version"), "src": src,
            "face_limit": inb.get("face_limit"), "quad": inb.get("quad"),
            "pbr": inb.get("pbr", True), "textured": inb.get("texture", True),
            "status": "running", "credits": 55,
            "src_image": ((inb.get("image") or {}).get("key") if isinstance(inb.get("image"), dict) else inb.get("image")),
        })
        db.log_event("model3d", "submitted", ref_id=op, account_sub=sub, status_code=code,
                     credits=55, duration_ms=dt, detail={"src": src, "model": inb.get("model_version")})
        sys.stderr.write("[db] model3d submitted %s\n" % op)

    # ---- 3D 进度轮询 ----
    elif p.endswith("/progress"):
        for it in (d if isinstance(d, list) else [d]):
            if not isinstance(it, dict):
                continue
            op = it.get("operator_id") or it.get("id")
            if not op:
                continue
            st, pg = it.get("status"), it.get("progress")
            if st in ("success", "failed"):
                db.upsert_model3d({"operator_id": op, "status": st, "progress": pg,
                                   "err": (it.get("error") if st == "failed" else None)})
                db.log_event("model3d", st, ref_id=op, account_sub=sub, status_code=code,
                             detail={"progress": pg})
                sys.stderr.write("[db] model3d %s %s\n" % (op, st))
            else:
                db.upsert_model3d({"operator_id": op, "status": st or "running", "progress": pg})

    # ---- 3D 详情：拿 GLB ----
    elif "/project/detail/v3/" in p:
        pr = d.get("project") or d
        if not isinstance(pr, dict):
            return
        op = None
        oper = pr.get("operator") or {}
        if isinstance(oper, dict):
            op = oper.get("operator_id")
        if not op:
            return
        url = pr.get("model_url")
        bucket, key = _s3_parts(url)
        itm = (oper.get("image_to_model") or {}) if isinstance(oper, dict) else {}
        db.upsert_model3d({
            "operator_id": op, "project_id": pr.get("id"),
            "name": pr.get("project_name"), "status": oper.get("status") or "success",
            "glb_url": url, "s3_bucket": bucket, "s3_key": key,
            "model_version": itm.get("model_version") or oper.get("model_version"),
            "face_limit": itm.get("face_limit"), "pbr": pr.get("is_pbr"), "textured": pr.get("is_textured"),
            "triangles": itm.get("triangles"),
        })
        sys.stderr.write("[db] model3d detail %s -> %s\n" % (op, (key or "")[-40:]))


def urlparse_path(p):
    return (p or "").split("?")[0]


def capture_from_browser(cdp_port=None):
    """通过 CDP 连接 9366 Chrome，抓取当前已登录账号（不接触密码）。"""
    cdp_port = cdp_port or CDP_PORT
    try:
        import websocket
    except Exception:
        return {"ok": False, "error": "缺少 websocket-client：pip install websocket-client"}
    import base64
    try:
        import urllib.request as ur
        tabs = json.load(ur.urlopen("http://127.0.0.1:%d/json/list" % cdp_port, timeout=8))
    except Exception as e:
        return {"ok": False, "error": "CDP %d 不可达：%s" % (cdp_port, e)}

    page = None
    for t in tabs:
        if t.get("type") == "page" and ("tripo3d.ai" in t.get("url", "") or "127.0.0.1:%d" % PORT in t.get("url", "")):
            page = t
            break
    if not page:
        return {"ok": False, "error": "9366 Chrome 里没有打开 studio.tripo3d.ai 页面"}

    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=45,
                                     max_size=100 * 1024 * 1024, suppress_origin=True)
    nid = [0]

    def cmd(method, **params):
        nid[0] += 1
        ws.send(json.dumps({"id": nid[0], "method": method, "params": params}))
        t0 = time.time()
        while time.time() - t0 < 40:
            msg = json.loads(ws.recv())
            if msg.get("id") == nid[0]:
                return msg
        raise TimeoutError(method)

    try:
        # 1) cookie
        cmd("Network.enable")
        ck = cmd("Network.getCookies", urls=["https://api.tripo3d.ai"]).get("result", {}).get("cookies", [])
        jar = {c["name"]: c["value"] for c in ck}
        cookie_names = [k for k in jar if k == "ory_kratos_session" or k.startswith("csrf_token_") or k == "tripo_device_id"]
        if "ory_kratos_session" not in jar:
            return {"ok": False, "error": "页面未登录（缺 ory_kratos_session）"}

        # 1.5) 必须切到 studio.tripo3d.ai 页面内执行 fetch（console 页跨域会被 CORS 拦）
        if "tripo3d.ai" not in page.get("url", ""):
            cmd("Page.enable")
            cmd("Page.navigate", url="https://studio.tripo3d.ai/zh/workspace/generate")
            t0 = time.time()
            while time.time() - t0 < 25:
                time.sleep(2)
                st = cmd("Runtime.evaluate", expression="document.readyState", returnByValue=True)
                if st.get("result", {}).get("result", {}).get("value") in ("interactive", "complete"):
                    break
            time.sleep(3)
            ck2 = cmd("Network.getCookies", urls=["https://api.tripo3d.ai"]).get("result", {}).get("cookies", [])
            jar = {c["name"]: c["value"] for c in ck2}
            cookie_names = [k for k in jar if k == "ory_kratos_session" or k.startswith("csrf_token_") or k == "tripo_device_id"]

        # 2) 换新 JWT
        js = ("(async()=>{const r=await fetch('https://api.tripo3d.ai/v2/studio/studio/whoami"
              "?tokenizeAs=default_jwt',{credentials:'include'});const d=await r.json();"
              "return JSON.stringify({status:r.status,tokenized:d.tokenized,"
              "email:(d.identity||{}).traits?(d.identity.traits.email||''):'',"
              "expires:d.expires_at});})()")
        r = cmd("Runtime.evaluate", expression=js, returnByValue=True, awaitPromise=True)
        val = r.get("result", {}).get("result", {}).get("value")
        info = json.loads(val) if isinstance(val, str) and val.startswith("{") else {}
        if not info:
            exc = r.get("result", {}).get("exceptionDetails")
            return {"ok": False, "error": "whoami 执行失败" + (("：" + json.dumps(exc)[:160]) if exc else "（返回非 JSON：" + str(val)[:120] + "）")}
        jwt = info.get("tokenized") or ""
        if not jwt:
            return {"ok": False, "error": "whoami 未返回 tokenized（status=%s，可能未登录）" % info.get("status")}
        # 3) 签发时间取 JWT payload
        sub, iat = "", 0
        try:
            p = jwt.split(".")[1]; p += "=" * (-len(p) % 4)
            pl = json.loads(base64.urlsafe_b64decode(p).decode())
            sub = pl.get("sub", ""); iat = pl.get("iat", 0)
        except Exception:
            pass
        return {"ok": True, "account": {
            "label": info.get("email") or ("sub:" + sub[:8] if sub else "captured"),
            "email": info.get("email", ""),
            "jwt": jwt,
            "device_id": jar.get("tripo_device_id", ""),
            "cookie": "; ".join("%s=%s" % (k, jar[k]) for k in cookie_names),
            "csrf": next((v for k, v in jar.items() if k.startswith("csrf_token_")), ""),
            "sub": sub,
            "added_at": time.time(),
            "kratos_expires": info.get("expires", ""),
            "jwt_iat": iat,
        }}
    except Exception as e:
        return {"ok": False, "error": "CDP 交互失败：%s" % e}
    finally:
        try:
            ws.close()
        except Exception:
            pass


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

    # ---------- static ----------
    def do_GET(self):
        p, qs = urlparse_path(self.path), parse_qs(urlparse(self.path).query)

        def _q(k, dv=None):
            v = qs.get(k, [None])[0]
            return v if v not in (None, "") else dv
        if p in ("/", "/index.html", "/console.html"):
            f = os.path.join(ROOT, "console.html")
            if not os.path.exists(f):
                return self._json(404, {"error": "console.html 不存在"})
            data = open(f, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if p == "/v1/accounts":
            return self._json(200, {"data": [probe_account(a) for a in load_pool()]})
        if p == "/v1/db/images":
            return self._json(200, {"data": db.list_images(limit=int(_q("limit", 50)),
                                                             account=_q("account"), status=_q("status"))})
        if p == "/v1/db/models3d":
            return self._json(200, {"data": db.list_models3d(limit=int(_q("limit", 50)),
                                                               account=_q("account"), status=_q("status"))})
        if p == "/v1/db/stats":
            return self._json(200, db.stats())
        if p == "/v1/pool/assets":
            # 跨账号聚合：并发拉每个账号的云端资产，附带归属账号信息
            incl = _q("include_preset") in ("1", "true", "yes")
            return self._json(*self.pool_assets(_q("type"), _q("q"), _q("limit"), _q("owner"), incl))
        if p == "/v1/pool/assets/export":
            code, body = self.pool_assets(None, None, 500, None, True)
            body["exported_at"] = time.time()
            return self._json(code, body)
        if p == "/v1/db/accounts":
            return self._json(200, {"data": db.list_accounts(with_secrets=False)})
        if p == "/v1/keepalive/status":
            return self._json(200, KA.status())
        if p == "/v1/db/artifacts":
            # 产物清单 + 存储状态（签名是否还有效）
            # source=cloud 时叠加云端实时资产（跨账号），并标注本地是否已入库
            return self._json(*self.list_artifacts(_q("kind"), _q("limit", "200"),
                                                   _q("source"), _q("account")))
        if p == "/v1/db/accounts/merged":
            # 账号池快照：DB 凭证 + 云端配额，供统一页顶部使用
            return self._json(200, {"data": db.list_accounts(with_secrets=False)})
        if p == "/v1/db/artifacts/fresh-url":
            # 按 asset_id / operator_id 换新签名 URL（不落盘，浏览器直连官方 CDN）
            return self._json(*self.fresh_url(_q("kind"), _q("id"), _q("download")))
        if p == "/v1/db/images/backfill":
            return self._json(*self.backfill_images(_q("account"), int(_q("pages", 3))))
        if p == "/v1/accounts/export":
            pool = db.list_accounts(with_secrets=True)
            return self._json(200, {"count": len(pool), "accounts": pool})
        return self.proxy("GET")

    def do_POST(self):
        p, qs = urlparse_path(self.path), parse_qs(urlparse(self.path).query)

        def _q(k, dv=None):
            v = qs.get(k, [None])[0]
            return v if v not in (None, "") else dv

        # multipart 由 handler 自行从 rfile 读，这里不要预读
        is_multipart = "multipart/form-data" in (self.headers.get("Content-Type") or "")
        n = int(self.headers.get("Content-Length") or 0)
        raw = b"" if is_multipart else (self.rfile.read(n) if n else b"")
        try:
            body = json.loads(raw) if raw else {}
        except Exception:
            body = {}

        if p == "/v1/accounts/capture":
            r = capture_from_browser(body.get("port") or CDP_PORT)
            if r.get("ok"):
                sub = db.upsert_account(r["account"])
                r["sub"] = sub
                r["pool_size"] = db.count_accounts()
            return self._json(200, r)

        if p == "/v1/upload/image":
            return self._json(*self.handle_upload(body))

        if p == "/v1/keepalive/tick":
            KA.tick()          # 手动立刻跑一次
            return self._json(200, {"ok": True, "status": KA.status()})

        if p == "/v1/accounts/remove":
            target = body.get("sub") or body.get("email") or ""
            n = db.delete_account(target)
            return self._json(200, {"ok": True, "removed": n, "left": db.count_accounts()})

        if p == "/v1/accounts/refresh-credits":
            results = []
            for a in load_pool():
                info = probe_account(a)
                if info.get("status") == 401:
                    new = refresh_jwt(a)
                    if new:
                        a["jwt"] = new
                        info = probe_account(a)
                        info["renewed"] = True
                info["sub"] = a.get("sub")
                info.pop("cookie", None)
                db.upsert_account(a)          # 凭证（jwt 可能已续签）
                db.upsert_account(info)       # 额度快照
                results.append(info)
            return self._json(200, {"results": results})

        if p == "/v1/accounts/renew-jwt":
            target = body.get("sub") or body.get("email") or ""
            hit = []
            for a in load_pool():
                if target and a.get("sub") != target and a.get("email") != target:
                    continue
                new = refresh_jwt(a)
                if new:
                    a["jwt"] = new
                    db.upsert_account(a)
                    hit.append(a.get("email") or (a.get("sub") or "?")[:8])
            return self._json(200, {"ok": bool(hit), "renewed": hit})

        return self.proxy("POST", raw)

    # ---------- proxy ----------
    def _pick_account(self, pool):
        """选一个可用账号：优先池内第一个；JWT 过期则先续签。"""
        if not pool:
            return None
        acct = pool[0]
        if len(pool) > 1 and self.headers.get("x-tripo-account"):
            t = self.headers["x-tripo-account"]
            acct = next((a for a in pool if a.get("sub") == t or a.get("email") == t), pool[0])
        # JWT 过期 -> 续签（whoami 靠 Kratos session，30 天有效）
        jwt = acct.get("jwt") or ""
        if jwt.count(".") == 2:
            import base64
            try:
                p = jwt.split(".")[1]; p += "=" * (-len(p) % 4)
                pl = json.loads(base64.urlsafe_b64decode(p).decode())
                if pl.get("exp", 0) < time.time():
                    new = refresh_jwt(acct)
                    if new:
                        acct["jwt"] = new
                        db.upsert_account(acct)
                        sys.stderr.write("[jwt] auto-renewed for %s\n" % (acct.get("email") or (acct.get("sub") or "")[:8]))
            except Exception:
                pass
        return acct

    def proxy(self, method, raw=None):
        pool = load_pool()
        if not pool:
            return self._json(502, {"error": {"message": "账号池为空，请先在「账号池」页从 9366 Chrome 捕获登录态",
                                              "code": "no_account"}})
        acct = self._pick_account(pool)

        path = self.path
        if path.startswith("/v1/studio/"):
            path = "/v2/studio/" + path[len("/v1/studio/"):]
        url = UPSTREAM + path
        headers = {
            "accept": "application/json",
            "origin": "https://studio.tripo3d.ai",
            "referer": "https://studio.tripo3d.ai/",
            "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36",
        }
        if acct.get("jwt"):
            headers["authorization"] = "Bearer " + acct["jwt"]
        if acct.get("device_id"):
            headers["x-tripo-device-id"] = acct["device_id"]
        headers["x-tripo-start"] = "%.1f" % (time.perf_counter() * 1000)
        if acct.get("cookie"):
            headers["cookie"] = acct["cookie"]
        if acct.get("csrf"):
            headers["csrf_token"] = acct["csrf"]
        if raw:
            headers["content-type"] = "application/json"

        req = urllib.request.Request(url, data=raw, headers=headers, method=method)
        t0 = time.time()
        try:
            resp = urllib.request.urlopen(req, timeout=120, context=SSL_CTX)
            data, code = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            data, code = e.read(), e.code
        except Exception as e:
            return self._json(502, {"error": {"message": "upstream error: %s" % e}})
        dt = int((time.time() - t0) * 1000)

        try:
            record_artifact(path, method, raw, code, data, acct, dt)
        except Exception as e:
            sys.stderr.write("[record] %s: %s\n" % (path, e))

        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def pool_assets(self, ftype=None, q=None, limit=None, owner=None, include_preset=False):
        """并发拉取账号池中每个账号的云端资产，聚合返回（含归属邮箱）。"""
        import concurrent.futures as cf
        pool = load_pool()
        if not pool:
            return 200, {"accounts": [], "projects": [], "totals": {}}

        def one(a):
            acct = self._pick_account_one(a)
            r = probe_assets(acct)
            r["account"] = {
                "sub": a.get("sub"), "email": a.get("email"), "label": a.get("label"),
                "plan": a.get("plan"), "credits": a.get("credits"),
                "expiring_date": a.get("expiring_date"),
            }
            return r

        with cf.ThreadPoolExecutor(max_workers=min(8, len(pool))) as ex:
            results = list(ex.map(one, pool))

        projects = []
        for r in results:
            for p in r.get("projects", []):
                p["owner_email"] = (r.get("account") or {}).get("email")
                p["owner_sub"] = (r.get("account") or {}).get("sub")
                p["owner_label"] = (r.get("account") or {}).get("label")
                p["owner_plan"] = (r.get("account") or {}).get("plan")
                projects.append(p)

        if not include_preset:
            # 过滤官方预置演示模型（type=preset，如 "Tripo Demo Model"）；
            # 它们占用 max_assets 配额但不是用户产物。按 type 判定而非名称，避免官方改名后漏掉。
            projects = [p for p in projects if (p.get("type") or "") != "preset"]
        if ftype:
            projects = [p for p in projects if (p.get("type") or "") == ftype]
        if owner:
            ol = owner.lower()
            projects = [p for p in projects if ol in (p.get("owner_email") or "").lower()
                        or ol in (p.get("owner_sub") or "").lower()
                        or ol in (p.get("owner_label") or "").lower()]
        if q:
            ql = q.lower()
            projects = [p for p in projects if ql in (p.get("name") or "").lower()
                        or ql in (p.get("model_version") or "").lower()
                        or ql in (p.get("owner_email") or "").lower()]
        projects.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        total_shown = len(projects)
        if limit:
            projects = projects[:int(limit)]

        return 200, {
            "accounts": [{
                "sub": (r.get("account") or {}).get("sub"),
                "email": (r.get("account") or {}).get("email"),
                "label": (r.get("account") or {}).get("label"),
                "plan": (r.get("account") or {}).get("plan"),
                "credits": (r.get("account") or {}).get("credits"),
                "ok": r.get("ok"), "error": r.get("error"),
                "total": r.get("total"), "max_assets": r.get("max_assets"),
                "shown": len([x for x in (r.get("projects") or [])
                               if include_preset or (x.get("type") or "") != "preset"]),
                "presets": len([x for x in (r.get("projects") or [])
                                if (x.get("type") or "") == "preset"]),
                "used_pct": (round(100.0 * r["total"] / r["max_assets"])
                             if r.get("total") is not None and r.get("max_assets") else None),
            } for r in results],
            "projects": projects,
            "totals": {
                "accounts": len(results),
                "accounts_ok": sum(1 for r in results if r.get("ok")),
                "projects": total_shown,
                "capacity": sum((r.get("max_assets") or 0) for r in results),
                "used": sum((r.get("total") or 0) for r in results),
                "presets": sum(len([x for x in (r.get("projects") or [])
                                    if (x.get("type") or "") == "preset"]) for r in results),
            },
        }

    def _pick_account_one(self, a):
        """给单个账号续签 JWT（pool_assets 并发场景用）。"""
        jwt = a.get("jwt") or ""
        if jwt.count(".") == 2:
            import base64
            try:
                p = jwt.split(".")[1]; p += "=" * (-len(p) % 4)
                pl = json.loads(base64.urlsafe_b64decode(p).decode())
                if pl.get("exp", 0) < time.time():
                    new = refresh_jwt(a)
                    if new:
                        a["jwt"] = new
                        db.upsert_account(a)
            except Exception:
                pass
        return a

    # ---------- upload ----------
    ALLOWED_IMG = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
                   "image/webp": "webp"}

    def _read_multipart(self):
        """解析 multipart/form-data，返回 (fields, files{name:(filename,bytes,headers)})。"""
        ctype = self.headers.get("Content-Type") or ""
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        if "multipart/form-data" not in ctype:
            return {}, {}
        m = re.search(r'boundary="?([^";]+)"?', ctype)
        if not m:
            return {}, {}
        bnd = ("--" + m.group(1)).encode()
        parts = raw.split(bnd)
        fields, files = {}, {}
        for part in parts[1:]:
            if part[:2] == b"--":
                break
            part = part.lstrip(b"\r\n")
            if b"\r\n\r\n" not in part:
                continue
            head, body = part.split(b"\r\n\r\n", 1)
            body = body[:-2] if body.endswith(b"\r\n") else body
            htxt = head.decode("utf-8", "replace")
            nm = re.search(r'name="([^"]+)"', htxt)
            fn = re.search(r'filename="([^"]*)"', htxt)
            if not nm:
                continue
            if fn:
                files[nm.group(1)] = (fn.group(1), body, htxt)
            else:
                fields[nm.group(1)] = body.decode("utf-8", "replace")
        return fields, files

    def handle_upload(self, body):
        """接收图片 -> 取 STS 临时凭证 -> 服务端代传 S3 -> 走审核 -> 返回 {bucket,key,audit}"""
        import awssign
        pool = load_pool()
        if not pool:
            return 502, {"error": {"message": "账号池为空"}}
        acct = self._pick_account(pool)

        fields, files = self._read_multipart()
        fentry = None
        for k in ("file", "image", "upload", "img"):
            if k in files:
                fentry = files[k]
                break
        if not fentry and "data_url" in body:
            # base64/data-url 兜底
            du = body["data_url"]
            m = re.match(r"^data:([^;]+);base64,(.+)$", du, re.S)
            if not m:
                return 400, {"error": {"message": "data_url 格式错误"}}
            ctype, b64 = m.group(1), m.group(2)
            fentry = ("inline." + self.ALLOWED_IMG.get(ctype, "png"), base64.b64decode(b64), ctype)
        if not fentry:
            return 400, {"error": {"message": "缺少文件（multipart 字段名 file，或 data_url）"}}
        fname, blob, chead = fentry

        ct = ""
        m = re.search(r"Content-Type:\s*([^\r\n]+)", chead or "", re.I)
        if m:
            ct = m.group(1).strip().lower()
        if ct not in self.ALLOWED_IMG:
            ct = self.ALLOWED_IMG.get((body.get("content_type") or "").lower(), "")
        if not ct:
            # 按魔数猜
            if blob[:8] == b"\x89PNG\r\n\x1a\n":
                ct = "image/png"
            elif blob[:3] == b"\xff\xd8\xff":
                ct = "image/jpeg"
            elif blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
                ct = "image/webp"
        if ct not in self.ALLOWED_IMG:
            return 400, {"error": {"message": "仅支持 PNG / JPEG / WebP（收到 %s）" % (ct or "未知类型")}}
        if len(blob) > 20 * 1024 * 1024:
            return 400, {"error": {"message": "文件超过 20MB（%.1fMB）" % (len(blob) / 1048576)}}
        ext = self.ALLOWED_IMG[ct]

        # 1) 取 STS 临时凭证
        code, data = call_upstream("/v2/studio/storage/temporary_token", "POST",
                                   json.dumps({"client": "aws", "format": ext}).encode(), acct)
        if code != 200:
            return 502, {"error": {"message": "取上传凭证失败 HTTP %d: %s" % (code, data[:160].decode("utf-8", "replace"))}}
        sts = json.loads(data).get("data", {})
        host = sts.get("host") or "s3.us-west-2.amazonaws.com"
        bucket = sts.get("resource_bucket") or "tripo-data"
        key = sts.get("resource_uri") or ("tripo-studio/%s/%s/input.%s" % (
            time.strftime("%Y%m%d"), db.now(), ext))
        region = "us-west-2"
        m = re.search(r"s3[.-]([a-z0-9-]+)\.amazonaws", host)
        if m:
            region = m.group(1)

        # 2) 服务端代传（STS 凭证不出服务端）
        put_url = awssign.presign_put(host, bucket, key, sts.get("sts_ak"), sts.get("sts_sk"),
                                      sts.get("session_token"), region, expires=900)
        try:
            preq = urllib.request.Request(put_url, data=blob, headers={"Content-Type": ct}, method="PUT")
            presp = urllib.request.urlopen(preq, timeout=120, context=SSL_CTX)
            up_code = presp.status
        except urllib.error.HTTPError as e:
            up_code = e.code
            up_body = e.read()[:300].decode("utf-8", "replace")
        except Exception as e:
            return 502, {"error": {"message": "S3 上传失败：%s" % e}}
        if up_code not in (200, 201, 204):
            return 502, {"error": {"message": "S3 返回 %d：%s" % (up_code, up_body)}}

        # 3) 审核（image_audit_result 是生图/3D 的必填项）
        code2, data2 = call_upstream("/v2/studio/audit/image", "POST",
                                     json.dumps({"image": {"bucket": bucket, "key": key}}).encode(), acct)
        audit = None
        if code2 == 200:
            audit = (json.loads(data2).get("data") or {}).get("result") or "pass"

        sys.stderr.write("[upload] %s %s %d bytes audit=%s\n" % (bucket, key, len(blob), audit))
        return 200, {"ok": True, "bucket": bucket, "key": key, "audit_result": audit,
                     "bytes": len(blob), "content_type": ct, "filename": fname}

    def backfill_images(self, account=None, pages=3):
        """从云端 image_assets 回填本地 images 表。

        背景：只有经过本代理的生成才会实时入库；在官网 UI 或直接调 api 生成的产物
        本地看不到。这里主动拉云端列表补齐（幂等，按 asset_id upsert）。
        """
        pool = load_pool()
        if not pool:
            return 502, {"error": {"message": "账号池为空"}}
        targets = pool
        if account:
            targets = [a for a in pool if a.get("sub") == account or a.get("email") == account]
            if not targets:
                return 404, {"error": {"message": "账号池中没有该账号"}}

        before = db.q("SELECT COUNT(*) c FROM images", one=True)["c"]
        added = updated = 0
        per_account = []
        for a in targets:
            acct = self._pick_account_one(a)
            got = 0
            for pn in range(1, max(1, pages) + 1):
                code, data = call_upstream(
                    "/v2/studio/image/image_assets", "POST",
                    json.dumps({"page_num": pn, "page_size": 20}).encode(), acct)
                if code != 200:
                    break
                try:
                    assets = (json.loads(data).get("data") or {}).get("assets") or []
                except Exception:
                    break
                if not assets:
                    break
                for it in assets:
                    aid = it.get("asset_id") or it.get("id")
                    if not aid:
                        continue
                    existed = db.q("SELECT id FROM images WHERE asset_id=?", (aid,), one=True)
                    inb = it.get("input") or {}
                    items = ((it.get("output") or {}).get("data")) or []
                    url = items[0].get("url") if items else None
                    bucket, key = _s3_parts(url)
                    db.upsert_image({
                        "asset_id": aid, "task_id": it.get("task_id"),
                        "account_sub": a.get("sub"), "account_email": a.get("email"),
                        "model_version": inb.get("model_version"),
                        "prompt": inb.get("prompt_text"),
                        "aspect_ratio": inb.get("scale"), "resolution": inb.get("resolution"),
                        "amount": inb.get("amount"), "template_id": inb.get("template_id"),
                        "t_pose": inb.get("t_pose"), "sketch": inb.get("sketch_to_render"),
                        "status": it.get("status") or "unknown",
                        "url": url, "s3_bucket": bucket, "s3_key": key,
                        "audit_result": (items[0].get("image_audit_result") if items else None),
                        "err": it.get("error"),
                    })
                    got += 1
                    if existed:
                        updated += 1
                    else:
                        added += 1
            per_account.append({"sub": a.get("sub"), "email": a.get("email"), "fetched": got})
        after = db.q("SELECT COUNT(*) c FROM images", one=True)["c"]
        return 200, {"ok": True, "added": added, "updated": updated,
                     "total_before": before, "total_after": after, "per_account": per_account}

    def list_artifacts(self, kind=None, limit=200, source=None, account=None):
        """统一产物视图：本地 DB（authoritative，带签名状态）+ 可选叠加云端实时资产。

        source:
          db     (默认) 只看本地 SQLite —— 本工具经手的产物
          cloud  只看云端 assets/v2 —— 账号全部资产（含官网 UI 生成的、preset）
          merged 两者合并，cloud 为准并标注是否已在本地入库（in_db）
        """
        try:
            limit = max(1, min(500, int(limit)))
        except Exception:
            limit = 200
        source = (source or "db").lower()
        local = []
        if kind in (None, "", "image"):
            for r in db.list_images(limit):
                st = classify_storage(r)
                local.append({
                    "kind": "image", "origin": "db", "in_db": True,
                    "id": r["asset_id"], "project_id": None,
                    "name": (r["prompt"] or "")[:80],
                    "model": r["model_version"], "status": r["status"], "credits": r["credits"],
                    "account": r["account_email"], "account_sub": r["account_sub"],
                    "s3_bucket": r["s3_bucket"], "s3_key": r["s3_key"],
                    "has_url": bool(r["url"]), "url": r["url"],
                    "cover": None, "src_image": None,
                    "created_at": r["created_at"],
                    "extra": {"amount": r["amount"], "aspect": r["aspect_ratio"],
                              "resolution": r["resolution"], "audit": r["audit_result"]},
                    "storage": st,
                })
        if kind in (None, "", "model3d"):
            for r in db.list_models3d(limit):
                st = classify_storage(r)
                local.append({
                    "kind": "model3d", "origin": "db", "in_db": True,
                    "id": r["operator_id"], "project_id": r["project_id"],
                    "name": r["name"] or "(未命名)",
                    "model": r["model_version"], "status": r["status"], "credits": r["credits"],
                    "account": r["account_email"], "account_sub": r["account_sub"],
                    "s3_bucket": r["s3_bucket"], "s3_key": r["s3_key"],
                    "has_url": bool(r["glb_url"]), "url": r["glb_url"],
                    "cover": None, "src_image": r["src_image"],
                    "created_at": r["created_at"],
                    "extra": {"progress": r["progress"], "face_limit": r["face_limit"],
                              "quad": r["quad"], "pbr": r["pbr"], "src": r["src"]},
                    "storage": st,
                })

        # 本地按 id 建索引，用于标注云端项是否已入库
        by_id = {}
        for x in local:
            by_id[(x["kind"], x["id"])] = x
            if x.get("project_id"):
                by_id[(x["kind"] + ":pid", x["project_id"])] = x

        cloud = []
        quotas = []
        if source in ("cloud", "merged"):
            pool = load_pool()
            targets = pool
            if account:
                targets = [a for a in pool if a.get("sub") == account or a.get("email") == account]
            for a in targets:
                acct = self._pick_account_one(a)
                r = probe_assets(acct)
                quotas.append({
                    "sub": a.get("sub"), "email": a.get("email"), "label": a.get("label"),
                    "plan": a.get("plan"), "credits": a.get("credits"),
                    "ok": r.get("ok"), "error": r.get("error"),
                    "total": r.get("total"), "max_assets": r.get("max_assets"),
                    "presets": len([x for x in (r.get("projects") or [])
                                    if (x.get("type") or "") == "preset"]),
                    "shown": len([x for x in (r.get("projects") or [])
                                  if (x.get("type") or "") != "preset"]),
                })
                for p in (r.get("projects") or []):
                    if kind and p.get("type") == "preset" and kind != "model3d":
                        pass
                    op = p.get("operator_id")
                    hit = by_id.get(("model3d", op)) or by_id.get(("model3d:pid", p.get("id")))
                    cloud.append({
                        "kind": "model3d", "origin": "cloud", "in_db": bool(hit),
                        "id": op or p.get("id"), "project_id": p.get("id"),
                        "name": p.get("name"), "type": p.get("type"),
                        "model": p.get("model_version"), "status": p.get("status"),
                        "credits": None,
                        "account": a.get("email"), "account_sub": a.get("sub"),
                        "account_plan": a.get("plan"),
                        "s3_bucket": p.get("s3_bucket"), "s3_key": p.get("s3_key"),
                        "has_url": bool(p.get("model_url")), "url": p.get("model_url"),
                        "cover": p.get("cover_image"), "src_image": p.get("src_image"),
                        "created_at": p.get("created_at"),
                        "extra": {"face_limit": p.get("face_limit"), "quad": p.get("quad"),
                                  "pbr": p.get("is_pbr"), "textured": p.get("is_textured"),
                                  "rigged": p.get("is_rigged"), "preset": p.get("type") == "preset"},
                        "storage": classify_storage({"url": p.get("model_url"), "s3_key": p.get("s3_key")}),
                    })

        if source == "db":
            items = local
        elif source == "cloud":
            items = cloud
        else:
            seen = set()
            items = []
            for x in cloud + local:      # 云端优先（同物去重）
                k = (x["kind"], x.get("s3_key") or x["id"])
                if k in seen:
                    continue
                seen.add(k)
                items.append(x)
        items.sort(key=lambda x: x.get("created_at") or 0, reverse=True)
        total_all = len(items)
        items = items[:limit]

        summary = {
            "total": total_all, "returned": len(items),
            "local": sum(1 for x in items if x["origin"] == "db"),
            "cloud": sum(1 for x in items if x["origin"] == "cloud"),
            "cloud_not_in_db": sum(1 for x in items if x["origin"] == "cloud" and not x["in_db"]),
            "signed_valid": sum(1 for x in items if x["storage"]["state"] == "signed_valid"),
            "signed_soon": sum(1 for x in items if x["storage"]["state"] == "signed_soon"),
            "key_only": sum(1 for x in items if x["storage"]["state"] == "key_only"),
            "no_key": sum(1 for x in items if x["storage"]["state"] == "no_key"),
        }
        return 200, {"artifacts": items, "summary": summary, "quotas": quotas,
                     "source": source}

    def fresh_url(self, kind, ref_id, download=None):
        """换一个新的签名 URL；可选 302 直跳，浏览器直接从官方 CDN 拉，不经过本机。"""
        if not ref_id:
            return 400, {"error": {"message": "缺少 id"}}
        pool = load_pool()
        if not pool:
            return 502, {"error": {"message": "账号池为空"}}
        acct = self._pick_account(pool)

        if kind == "model3d":
            row = db.q("SELECT * FROM models_3d WHERE operator_id=?", (ref_id,), one=True)
            pid = (row or {}).get("project_id") or ref_id
            url, err = fetch_fresh_url_3d(acct, pid)
        else:
            row = db.q("SELECT * FROM images WHERE asset_id=?", (ref_id,), one=True)
            url, err = fetch_fresh_url(acct, "image", ref_id)

        if not url:
            return 404, {"error": {"message": err or "无法取得 url"}}
        st = classify_storage({"url": url, "s3_key": (row or {}).get("s3_key")})
        # 顺手把新 URL 落库（保持 DB 里的链接尽量新）
        try:
            b, k = _s3_parts(url)
            if kind == "model3d":
                db.upsert_model3d({"operator_id": ref_id, "glb_url": url, "s3_bucket": b, "s3_key": k})
            else:
                db.upsert_image({"asset_id": ref_id, "url": url, "s3_bucket": b, "s3_key": k})
        except Exception:
            pass

        if download in ("1", "true", "yes"):
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        return 200, {"ok": True, "url": url, "storage": st,
                     "redirect_hint": "/v1/db/artifacts/fresh-url?kind=%s&id=%s&download=1" % (kind, ref_id)}

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


KA = ka_mod.KeepAlive(load_pool, refresh_jwt, probe_account)

if __name__ == "__main__":
    print("tripo2api console  ->  http://127.0.0.1:%d" % PORT)
    print("db: %s" % db.init())
    print("accounts: %d" % db.count_accounts())
    print("cdp: 127.0.0.1:%d (仅用于首次捕获登录态)" % CDP_PORT)
    th = KA.start()
    print("keepalive: %s" % ("running (JWT/%ds 额度/%ds)" % (ka_mod.RENEW_INTERVAL, ka_mod.CREDIT_INTERVAL)
                              if th else "disabled"))
    try:
        ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
    except KeyboardInterrupt:
        KA.stop()


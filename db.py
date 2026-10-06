#!/usr/bin/env python3
"""
tripo2api 持久层：SQLite（WAL 模式）

表：
  accounts      账号池（Kratos session + JWT + 设备指纹 + 实时额度快照）
  images        图像生成产物（asset_id / url / model / prompt / 审核结果）
  models_3d     3D 生成产物（operator_id / project_id / glb url / 面数 / 贴图）
  generation_log 统一生成流水（类型/状态/耗时/credits，用于统计与排障）
"""
import json, os, sqlite3, threading, time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
DB_FILE = os.path.join(DATA_DIR, "tripo2api.db")
LEGACY_ACCOUNTS = os.path.join(ROOT, "accounts.json")

_lock = threading.RLock()
_conn = None

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 账号池：sub 为主键（Ory Kratos identity id），email 唯一可空
CREATE TABLE IF NOT EXISTS accounts (
  sub          TEXT PRIMARY KEY,
  email        TEXT UNIQUE,
  label        TEXT,
  jwt          TEXT,
  device_id    TEXT,
  cookie       TEXT,
  csrf         TEXT,
  kratos_expires TEXT,
  plan         TEXT,
  free_trial   INTEGER DEFAULT 0,
  credits      INTEGER,
  expiring_credit INTEGER,
  expiring_date   TEXT,
  member_end     TEXT,
  invite_code    TEXT,
  quota_smart_mesh INTEGER,
  quota_uv_edit   INTEGER,
  assets        INTEGER,
  assets_max    INTEGER,
  last_probe    INTEGER,
  created_at    INTEGER,
  updated_at    INTEGER
);
CREATE INDEX IF NOT EXISTS accounts_created ON accounts(created_at DESC);

-- 图像产物
CREATE TABLE IF NOT EXISTS images (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  asset_id      TEXT UNIQUE,        -- 上游 asset_id（幂等键）
  task_id       TEXT,
  account_sub   TEXT,
  account_email TEXT,
  model_version TEXT,
  prompt        TEXT,
  aspect_ratio  TEXT,
  resolution    TEXT,
  amount        INTEGER DEFAULT 1,
  template_id   TEXT,
  t_pose        INTEGER DEFAULT 0,
  sketch        INTEGER DEFAULT 0,
  status        TEXT,               -- running/success/failed
  credits       INTEGER,
  audit_result  TEXT,
  url           TEXT,               -- 产物签名 URL
  s3_bucket     TEXT,
  s3_key        TEXT,
  err           TEXT,
  created_at    INTEGER,
  updated_at    INTEGER,
  FOREIGN KEY (account_sub) REFERENCES accounts(sub) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS images_created ON images(created_at DESC);
CREATE INDEX IF NOT EXISTS images_account ON images(account_sub);

-- 3D 产物
CREATE TABLE IF NOT EXISTS models_3d (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  operator_id   TEXT UNIQUE,        -- 上游 operator_id（幂等键）
  project_id    TEXT,
  account_sub   TEXT,
  account_email TEXT,
  name          TEXT,
  model_version TEXT,
  src           TEXT,               -- image_to_model / text_to_model / ...
  face_limit    INTEGER,
  triangles     INTEGER,
  quad          INTEGER DEFAULT 0,
  pbr           INTEGER DEFAULT 1,
  textured      INTEGER DEFAULT 1,
  status        TEXT,               -- running/success/failed
  progress      INTEGER,
  credits       INTEGER,
  glb_url       TEXT,
  s3_bucket     TEXT,
  s3_key        TEXT,
  src_image     TEXT,               -- 输入图 key
  err           TEXT,
  created_at    INTEGER,
  updated_at    INTEGER,
  FOREIGN KEY (account_sub) REFERENCES accounts(sub) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS models3d_created ON models_3d(created_at DESC);
CREATE INDEX IF NOT EXISTS models3d_account ON models_3d(account_sub);

-- 统一流水（提交/完成/失败事件级）
CREATE TABLE IF NOT EXISTS generation_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  at          INTEGER NOT NULL,
  kind        TEXT NOT NULL,        -- image / model3d
  ref_id      TEXT,                 -- asset_id 或 operator_id
  event       TEXT NOT NULL,        -- submitted / progress / success / failed
  account_sub TEXT,
  status_code INTEGER,
  credits     INTEGER,
  duration_ms INTEGER,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS genlog_at ON generation_log(at DESC);
CREATE INDEX IF NOT EXISTS genlog_kind ON generation_log(kind, event);
"""


def now() -> int:
    return int(time.time())


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def q(sql, args=(), one=False):
    with _lock:
        cur = conn().execute(sql, args)
        rows = cur.fetchall()
        cur.close()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]


def ex(sql, args=()):
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid, cur.rowcount


def exmany(sql, seq):
    with _lock:
        c = conn()
        c.executemany(sql, seq)
        c.commit()


def jload(s, default=None):
    try:
        return json.loads(s) if s else (default if default is not None else {})
    except Exception:
        return default if default is not None else {}


# ---------------------------------------------------------------- accounts
def upsert_account(a: dict):
    """插入或更新账号。

    幂等键优先 sub；若只有 email 则用 email 作 sub。若两者都缺则用 device_id。
    email 上的 UNIQUE 约束要求：写入时 email 不能与其它行冲突（NULL 允许重复）。
    """
    sub = a.get("sub") or ("email:" + a["email"] if a.get("email") else
                           ("dev:" + a.get("device_id", "") if a.get("device_id") else None))
    if not sub:
        return None

    # sub 变化（例如从 dev:/email: 前缀切到真实 Kratos sub）时，尝试迁移旧行
    old = q("SELECT sub FROM accounts WHERE email=? AND sub<>? LIMIT 1",
            (a.get("email"), sub), one=True) if a.get("email") else None
    if old:
        ex("UPDATE OR REPLACE accounts SET sub=? WHERE sub=?", (sub, old["sub"]))
        # 关联的产物指向新 sub
        ex("UPDATE images SET account_sub=? WHERE account_sub=?", (sub, old["sub"]))
        ex("UPDATE models_3d SET account_sub=? WHERE account_sub=?", (sub, old["sub"]))
        ex("UPDATE generation_log SET account_sub=? WHERE account_sub=?", (sub, old["sub"]))

    t = now()
    cand = {
        "email": a.get("email") or None,
        "label": a.get("label") or (a.get("email") or sub[:12]),
        "jwt": a.get("jwt"),
        "device_id": a.get("device_id"),
        "cookie": a.get("cookie"),
        "csrf": a.get("csrf"),
        "kratos_expires": a.get("kratos_expires"),
        "plan": a.get("plan"),
        "free_trial": (1 if a.get("free_trial") else 0) if a.get("free_trial") is not None else None,
        "credits": a.get("credits"),
        "expiring_credit": a.get("expiring_credit"),
        "expiring_date": a.get("expiring_date"),
        "member_end": a.get("member_end"),
        "invite_code": a.get("invite_code"),
        "quota_smart_mesh": a.get("quota_smart_mesh"),
        "quota_uv_edit": a.get("quota_uv_edit"),
        "assets": a.get("assets"),
        "assets_max": a.get("assets_max"),
    }
    # 关键：值缺省(None)时保留原值，不覆盖 —— 探测快照不携带凭证，不能把 jwt/cookie 抹掉
    exists = get_account(sub)
    if exists:
        fields = {k: v for k, v in cand.items() if v is not None or k in ("email", "invite_code")}
        fields["last_probe"] = t
        fields["updated_at"] = t
    else:
        fields = dict(cand)
        fields["label"] = fields.get("label") or sub[:12]
        fields["last_probe"] = t
        fields["updated_at"] = t

    keys = list(fields)
    sets = ",".join("%s=excluded.%s" % (k, k) for k in keys)
    ex("""INSERT INTO accounts (sub, created_at, %s) VALUES (?,?,%s)
          ON CONFLICT(sub) DO UPDATE SET %s""" % (",".join(keys), ",".join(["?"] * len(keys)), sets),
        tuple([sub, t] + [fields[k] for k in keys]))
    return sub


def list_accounts(with_secrets: bool = False):
    rows = q("SELECT * FROM accounts ORDER BY created_at ASC")
    if not with_secrets:
        for r in rows:
            for k in ("jwt", "cookie", "csrf"):
                r.pop(k, None)
    return rows


def get_account(sub: str):
    return q("SELECT * FROM accounts WHERE sub=?", (sub,), one=True)


def delete_account(sub: str):
    _, n = ex("DELETE FROM accounts WHERE sub=?", (sub,))
    return n


def count_accounts():
    return q("SELECT COUNT(*) c FROM accounts", one=True)["c"]


# ---------------------------------------------------------------- images
def upsert_image(d: dict):
    """按 asset_id 幂等写入图像产物。"""
    aid = d.get("asset_id")
    if not aid:
        return None
    t = now()
    existing = q("SELECT id, created_at, credits FROM images WHERE asset_id=?", (aid,), one=True)
    if existing:
        ex("""UPDATE images SET task_id=COALESCE(?,task_id), model_version=COALESCE(?,model_version),
              prompt=COALESCE(?,prompt), status=COALESCE(?,status), url=COALESCE(?,url),
              s3_bucket=COALESCE(?,s3_bucket), s3_key=COALESCE(?,s3_key), audit_result=COALESCE(?,audit_result),
              err=COALESCE(?,err), credits=COALESCE(?,credits), updated_at=?
             WHERE asset_id=?""",
           (d.get("task_id"), d.get("model_version"), d.get("prompt"), d.get("status"), d.get("url"),
            d.get("s3_bucket"), d.get("s3_key"), d.get("audit_result"), d.get("err"), d.get("credits"),
            t, aid))
        return existing["id"]
    _, iid = ex("""INSERT INTO images
        (asset_id, task_id, account_sub, account_email, model_version, prompt, aspect_ratio,
         resolution, amount, template_id, t_pose, sketch, status, credits, audit_result,
         url, s3_bucket, s3_key, err, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (aid, d.get("task_id"), d.get("account_sub"), d.get("account_email"), d.get("model_version"),
         d.get("prompt"), d.get("aspect_ratio"), d.get("resolution"), d.get("amount") or 1,
         d.get("template_id"), 1 if d.get("t_pose") else 0, 1 if d.get("sketch") else 0,
         d.get("status") or "running", d.get("credits"), d.get("audit_result"),
         d.get("url"), d.get("s3_bucket"), d.get("s3_key"), d.get("err"), t, t))
    return iid


def list_images(limit=50, account=None, status=None):
    sql = "SELECT * FROM images WHERE 1=1"
    a = []
    if account:
        sql += " AND (account_sub=? OR account_email=?)"; a += [account, account]
    if status:
        sql += " AND status=?"; a.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"; a.append(limit)
    return q(sql, tuple(a))


# ---------------------------------------------------------------- models_3d
def upsert_model3d(d: dict):
    """按 operator_id 幂等写入 3D 产物。"""
    op = d.get("operator_id")
    if not op:
        return None
    t = now()
    existing = q("SELECT id FROM models_3d WHERE operator_id=?", (op,), one=True)
    if existing:
        ex("""UPDATE models_3d SET project_id=COALESCE(?,project_id), name=COALESCE(?,name),
              status=COALESCE(?,status), progress=COALESCE(?,progress), glb_url=COALESCE(?,glb_url),
              s3_bucket=COALESCE(?,s3_bucket), s3_key=COALESCE(?,s3_key), triangles=COALESCE(?,triangles),
              credits=COALESCE(?,credits), err=COALESCE(?,err), updated_at=? WHERE operator_id=?""",
           (d.get("project_id"), d.get("name"), d.get("status"), d.get("progress"), d.get("glb_url"),
            d.get("s3_bucket"), d.get("s3_key"), d.get("triangles"), d.get("credits"), d.get("err"), t, op))
        return existing["id"]
    _, iid = ex("""INSERT INTO models_3d
        (operator_id, project_id, account_sub, account_email, name, model_version, src, face_limit,
         triangles, quad, pbr, textured, status, progress, credits, glb_url, s3_bucket, s3_key,
         src_image, err, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (op, d.get("project_id"), d.get("account_sub"), d.get("account_email"), d.get("name"),
         d.get("model_version"), d.get("src"), d.get("face_limit"), d.get("triangles"),
         1 if d.get("quad") else 0, 1 if d.get("pbr", True) else 0, 1 if d.get("textured", True) else 0,
         d.get("status") or "running", d.get("progress"), d.get("credits"), d.get("glb_url"),
         d.get("s3_bucket"), d.get("s3_key"), d.get("src_image"), d.get("err"), t, t))
    return iid


def list_models3d(limit=50, account=None, status=None):
    sql = "SELECT * FROM models_3d WHERE 1=1"
    a = []
    if account:
        sql += " AND (account_sub=? OR account_email=?)"; a += [account, account]
    if status:
        sql += " AND status=?"; a.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"; a.append(limit)
    return q(sql, tuple(a))


# ---------------------------------------------------------------- log
def log_event(kind, event, ref_id=None, account_sub=None, status_code=None,
              credits=None, duration_ms=None, detail=None):
    ex("""INSERT INTO generation_log (at, kind, ref_id, event, account_sub, status_code, credits, duration_ms, detail)
          VALUES (?,?,?,?,?,?,?,?,?)""",
       (now(), kind, ref_id, event, account_sub, status_code, credits, duration_ms,
        json.dumps(detail, ensure_ascii=False) if not isinstance(detail, (str, type(None))) else detail))


def stats():
    return {
        "accounts": count_accounts(),
        "images": q("SELECT COUNT(*) c, COALESCE(SUM(credits),0) s FROM images WHERE status='success'", one=True),
        "models3d": q("SELECT COUNT(*) c, COALESCE(SUM(credits),0) s FROM models_3d WHERE status='success'", one=True),
        "running": q("SELECT (SELECT COUNT(*) FROM images WHERE status='running') + "
                     "(SELECT COUNT(*) FROM models_3d WHERE status='running') c", one=True)["c"],
    }


# ---------------------------------------------------------------- 迁移
def migrate_legacy():
    """把旧的 accounts.json 迁进 SQLite（幂等）。"""
    if not os.path.exists(LEGACY_ACCOUNTS):
        return 0
    try:
        with open(LEGACY_ACCOUNTS) as f:
            data = json.load(f)
    except Exception:
        return 0
    if isinstance(data, dict):
        data = data.get("accounts", [])
    n = 0
    for a in data:
        if upsert_account(a):
            n += 1
    if n:
        os.rename(LEGACY_ACCOUNTS, LEGACY_ACCOUNTS + ".migrated")
    return n


def init():
    conn()
    migrate_legacy()
    return DB_FILE


if __name__ == "__main__":
    p = init()
    print("db:", p)
    for t in ("accounts", "images", "models_3d", "generation_log"):
        cols = [r["name"] for r in q("PRAGMA table_info(%s)" % t)]
        print("  %-15s %2d cols" % (t, len(cols)))
    print("stats:", json.dumps(stats(), ensure_ascii=False))

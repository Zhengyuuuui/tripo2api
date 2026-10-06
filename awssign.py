#!/usr/bin/env python3
"""
最小 AWS SigV4 签名器：只支持 S3 PUT（UNSIGNED-PAYLOAD），用于把用户上传的图片
直传到 Tripo 的 S3（bucket=tripo-data）。

无需 boto3。凭证来自 /v2/studio/storage/temporary_token 返回的 STS 临时三元组。
"""
import datetime, hashlib, hmac
from urllib.parse import quote, urlsplit

SERVICE = "s3"
ALGO = "AWS4-HMAC-SHA256"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret: str, datestamp: str, region: str, service: str = SERVICE) -> bytes:
    k = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, "aws4_request")


def presign_put(host, bucket, key, ak, sk, session_token, region, expires=900):
    """生成 presigned PUT URL（S3 虚拟主机风格）。

    注意：canonical query string 必须按 key 的 UTF-8 字节序排序（S3 会重排后再验签），
    所以 Security-Token 排在 SignedHeaders 之前。
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    credential_scope = "%s/%s/%s/aws4_request" % (datestamp, region, SERVICE)

    # 虚拟主机风格：https://<bucket>.s3.<region>.amazonaws.com/<key>
    host = "%s.s3.%s.amazonaws.com" % (bucket, region)
    canonical_uri = "/" + quote(key.lstrip("/"), safe="/~")
    signed_headers = "host"

    q = {
        "X-Amz-Algorithm": quote(ALGO, safe=""),
        "X-Amz-Credential": quote("%s/%s" % (ak, credential_scope), safe=""),
        "X-Amz-Date": amzdate,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": signed_headers,
    }
    if session_token:
        q["X-Amz-Security-Token"] = quote(session_token, safe="")
    # 按 key 排序后拼接（AWS 规范要求）
    canonical_qs = "&".join("%s=%s" % (k, q[k]) for k in sorted(q))

    canonical_req = "\n".join(["PUT", canonical_uri, canonical_qs,
                               "host:%s\n" % host, signed_headers, "UNSIGNED-PAYLOAD"])
    string_to_sign = "\n".join([ALGO, amzdate, credential_scope,
                                hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()])
    sig = hmac.new(signing_key(sk, datestamp, region), string_to_sign.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return "https://%s%s?%s&X-Amz-Signature=%s" % (host, canonical_uri, canonical_qs, sig)


def presign_put_host_style(host, path, ak, sk, session_token, region, expires=900):
    """path-style：https://<host>/<path>?..."""
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    credential_scope = "%s/%s/%s/aws4_request" % (datestamp, region, SERVICE)
    q = {
        "X-Amz-Algorithm": quote(ALGO, safe=""),
        "X-Amz-Credential": quote("%s/%s" % (ak, credential_scope), safe=""),
        "X-Amz-Date": amzdate,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if session_token:
        q["X-Amz-Security-Token"] = quote(session_token, safe="")
    canonical_qs = "&".join("%s=%s" % (k, q[k]) for k in sorted(q))
    canonical_req = "\n".join(["PUT", path, canonical_qs, "host:%s\n" % host, "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join([ALGO, amzdate, credential_scope,
                     hashlib.sha256(canonical_req.encode("utf-8")).hexdigest()])
    sig = hmac.new(signing_key(sk, datestamp, region), sts.encode("utf-8"), hashlib.sha256).hexdigest()
    return "https://%s%s?%s&X-Amz-Signature=%s" % (host, path, canonical_qs, sig)

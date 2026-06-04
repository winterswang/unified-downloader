#!/usr/bin/env python3
"""
将下载的年报 PDF 同步到 IMA 知识库

用法:
    python sync_to_ima.py <PDF路径> <知识库ID> [文件名]
    python sync_to_ima.py ./downloads/a/600/600519_2024_ANNUAL_REPORT.PDF KB123456

前提:
    - IMA 凭证: ~/.config/ima/client_id 和 ~/.config/ima/api_key
    - 或环境变量: IMA_OPENAPI_CLIENTID, IMA_OPENAPI_APIKEY
"""
import sys
import os
import json
import ssl
import urllib.request
import subprocess
from pathlib import Path

# ---- 路径解析（跨平台兼容） ----
_HOME = Path.home()
_IMA_CONFIG_DIR = _HOME / ".config" / "ima"
_COS_UPLOAD_CANDIDATES = [
    os.environ.get("COS_UPLOAD_SCRIPT"),
    str(_HOME / ".hermes" / "skills" / "ima" / "knowledge-base" / "scripts" / "cos-upload.cjs"),
    str(_HOME / ".openclaw" / "workspace" / "skills" / "ima-skills" / "knowledge-base" / "scripts" / "cos-upload.cjs"),
]


def _read_credential(filename: str) -> str:
    """从 ~/.config/ima/ 读取凭证文件，跨平台安全"""
    path = _IMA_CONFIG_DIR / filename
    if path.exists():
        return path.read_text().strip()
    return ""


# ---- 配置 ----
IMA_CLIENT_ID = os.environ.get("IMA_OPENAPI_CLIENTID") or _read_credential("client_id")
IMA_API_KEY = os.environ.get("IMA_OPENAPI_APIKEY") or _read_credential("api_key")

BASE_URL = "https://ima.qq.com"
CTX = ssl.create_default_context()

# cos-upload.cjs 路径：优先级 环境变量 > ~/.hermes/skills/... > ~/.openclaw/...（Docker 回退）
COS_UPLOAD_SCRIPT = next((p for p in _COS_UPLOAD_CANDIDATES if p and Path(p).exists()), "")


def api(path: str, body: dict) -> dict:
    """调用 IMA OpenAPI"""
    if not IMA_CLIENT_ID or not IMA_API_KEY:
        raise RuntimeError(
            "缺少 IMA 凭证。请配置:\n"
            "  - 环境变量: IMA_OPENAPI_CLIENTID, IMA_OPENAPI_APIKEY\n"
            "  - 或文件: ~/.config/ima/client_id, ~/.config/ima/api_key"
        )

    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "ima-openapi-clientid": IMA_CLIENT_ID,
            "ima-openapi-apikey": IMA_API_KEY,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(req, context=CTX, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def sync_pdf(pdf_path: str, knowledge_base_id: str, file_name: str = None) -> str:
    """
    将 PDF 文件同步到 IMA 知识库

    流程:
      1. check_repeated_names  — 检查是否重名
      2. create_media           — 获取 media_id 和 COS 上传凭证
      3. COS PUT (via node)     — 上传文件到腾讯云 COS（使用 cos-upload.cjs）
      4. add_knowledge           — 通知知识库入库（用 media_id，非 cos_key）

    Returns:
        media_id (str): 知识库媒体 ID

    Raises:
        RuntimeError: 凭证缺失或 API 返回错误
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"文件不存在: {pdf_path}")

    file_name = file_name or os.path.basename(pdf_path)
    file_size = os.path.getsize(pdf_path)
    print(f"[同步] {file_name} ({file_size:,} bytes) → 知识库 {knowledge_base_id}")

    # ---- Step 1: 检查重名 ----
    r = api("/openapi/wiki/v1/check_repeated_names", {
        "params": [{"name": file_name, "media_type": 1}],
        "knowledge_base_id": knowledge_base_id,
    })
    repeated = r.get("data", {}).get("repeated_names", [])
    if repeated:
        print(f"  ⚠️  知识库中已存在: {[x['name'] for x in repeated]}")
    else:
        print(f"  ✅ 无重名，继续")

    # ---- Step 2: 创建媒体，获取 media_id 和 COS 凭证 ----
    r = api("/openapi/wiki/v1/create_media", {
        "knowledge_base_id": knowledge_base_id,
        "file_ext": "pdf",
        "content_type": "application/pdf",
        "file_size": file_size,
    })
    data = r["data"]
    media_id = data["media_id"]
    cos = data["cos_credential"]
    cos_key = cos["cos_key"]
    token = cos["token"]
    secret_id = cos["secret_id"]
    secret_key = cos["secret_key"]
    bucket = cos["bucket_name"]
    region = cos["region"]

    print(f"  ✅ 凭证获取成功: media_id={media_id[:40]}...")

    # ---- Step 3: 上传 PDF 到 COS (使用 node cos-upload.cjs) ----
    if not COS_UPLOAD_SCRIPT:
        raise RuntimeError(
            "未找到 cos-upload.cjs，请:\n"
            "  - 设置环境变量 COS_UPLOAD_SCRIPT=<路径>\n"
            "  - 或将 ima-skills 安装到 ~/.hermes/skills/ima/"
        )
    print(f"  上传中 ({file_size:,} bytes)...")
    proc = subprocess.Popen(
        ["node", COS_UPLOAD_SCRIPT,
         "--file", pdf_path,
         "--secret-id", secret_id,
         "--secret-key", secret_key,
         "--token", token,
         "--bucket", bucket,
         "--region", region,
         "--cos-key", cos_key,
         "--content-type", "application/pdf"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    stdout, stderr = proc.communicate()
    out = stdout.decode().strip()
    if "successful" not in out.lower():
        raise RuntimeError(f"COS 上传失败: {out} {stderr.decode()[:200]}")
    print(f"  ✅ COS 上传完成")

    # ---- Step 4: 通知知识库添加文件（使用 media_id） ----
    r = api("/openapi/wiki/v1/add_knowledge", {
        "knowledge_base_id": knowledge_base_id,
        "media_id": media_id,
        "media_type": 1,
        "title": file_name,
        "file_info": {
            "cos_key": cos_key,
            "file_name": file_name,
            "file_size": file_size,
            "last_modify_time": int(os.path.getmtime(pdf_path)),
        },
    })
    if r["code"] != 0:
        raise RuntimeError(f"add_knowledge 失败: code={r['code']} msg={r['msg']}")

    kid = r.get("data", {}).get("knowledge_id", "N/A")
    print(f"  ✅ 已添加到 IMA 知识库: media_id={media_id[:40]}...")
    return media_id


def main():
    if len(sys.argv) < 3:
        print("用法:")
        print("  python sync_to_ima.py <PDF路径> <知识库ID> [文件名]")
        print("  python sync_to_ima.py ./downloads/a/600/600519_2024_ANNUAL_REPORT.PDF KB123456")
        sys.exit(1)

    pdf_path = sys.argv[1]
    kb_id = sys.argv[2]
    fname = sys.argv[3] if len(sys.argv) > 3 else None

    try:
        mid = sync_pdf(pdf_path, kb_id, fname)
        print(f"\n🎉 完成! media_id={mid}")
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 同步失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
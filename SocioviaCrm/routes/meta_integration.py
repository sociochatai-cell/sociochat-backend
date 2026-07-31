import os
import json
import logging
import requests
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, current_app
from google import genai
from google import genai
from google.genai import types
from google.genai.types import HttpOptions
from MetaHelpers.GetWorkspaceData import get_social_accounts_for_workspace_user, discover_ad_accounts_for_token
from rate_limit.decorator import rate_limit

# Initialize Blueprint
bp = Blueprint("meta_integration", __name__)

# Meta API Configuration
FB_API_VERSION = os.getenv("FB_API_VERSION", "v22.0")
BASE_URL = f"https://graph.facebook.com/{FB_API_VERSION}"

# Google GenAI Configuration - Vertex AI Mode
def init_client():
    # Prefer simple API-key mode when a Gemini API key is configured — it needs
    # no service-account JSON. Falls back to Vertex AI (project + ADC) below.
    api_key = (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()
    if api_key:
        # 1) Gemini Developer API key (the usual "AIza…" keys)
        try:
            client = genai.Client(api_key=api_key)
            logger.info("[startup] genai.Client initialised in API-key mode")
            return client
        except Exception as e:
            logger.warning(f"[startup] genai API-key mode failed: {e}")
        # 2) Vertex AI Express key (keys that look like "AQ.…")
        try:
            client = genai.Client(api_key=api_key, vertexai=True)
            logger.info("[startup] genai.Client initialised in Vertex Express mode")
            return client
        except Exception as e:
            logger.warning(f"[startup] genai Vertex-Express mode failed, trying full Vertex: {e}")

    project = os.environ.get("GCP_PROJECT") or os.environ.get("PROJECT_ID") or "angular-sorter-473216-k8"
    location = os.environ.get("GOOGLE_CLOUD_LOCATION") or "global"
    adc_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    # logger.debug(f"[env] GCP_PROJECT: {project}")
    # logger.debug(f"[env] LOCATION: {location}")
    # logger.debug(f"[env] ADC PATH SET: {bool(adc_path)}")
    try:
        # If ADC path is not set, this might fail unless default credentials work
        # but the user code implies they rely on these env vars.
        client = genai.Client(
            http_options=HttpOptions(api_version="v1"),
            project=project,
            location=location,
            vertexai=True,
        )
        return client
    except Exception as e:
        logger.error(f"[startup] genai.Client init FAILED: {e}")
        return None

# Global client holder - initialized lazily
GENAI_CLIENT = None

def get_genai_client():
    global GENAI_CLIENT
    if GENAI_CLIENT is None:
        GENAI_CLIENT = init_client()
    return GENAI_CLIENT

logger = logging.getLogger("sociovia.meta_integration")

GENAI_MODEL = os.getenv("TEXT_MODEL", "gemini-3.1-flash-lite")  # Read from env, fallback to stable model
GENAI_TEMPERATURE = 0.7
GENAI_MAX_TOKENS = 1000

def _download_image_as_bytes(url):
    try:
        r = requests.get(url, timeout=10)
        if r.ok:
            return r.content, r.headers.get("Content-Type", "image/jpeg")
    except Exception:
        pass
    return None, None

def _generate_content_robust(contents, temperature=0.7):
    client = get_genai_client()
    if not client:
        raise RuntimeError("GenAI client not initialized")
    
    config = types.GenerateContentConfig(
        temperature=temperature,
        top_p=1.0,
        max_output_tokens=GENAI_MAX_TOKENS,
        response_mime_type="application/json",
    )
    
    # Debug: Log which model is being used
    logger.info(f"[GenAI] Using model: {GENAI_MODEL}")
    
    # Vertex AI client usage
    response = client.models.generate_content(
        model=GENAI_MODEL,
        contents=contents,
        config=config
    )
    
    # Debug: Log raw response
    try:
        logger.info(f"[GenAI] Raw response text: {response.text[:500] if response.text else 'EMPTY'}")
    except Exception as e:
        logger.warning(f"[GenAI] Could not extract response text: {e}")
    
    return response

def _extract_text_from_genai_response(resp):
    try:
        return resp.text
    except Exception:
        return ""

def _extract_json_from_textt(text):
    if not text:
        return None
    try:
        # Try direct load
        return json.loads(text)
    except:
        pass
    
    # Try markdown block
    try:
        if "```json" in text:
            cleaned = text.split("```json")[1].split("```")[0].strip()
            return json.loads(cleaned)
        elif "```" in text:
            cleaned = text.split("```")[1].strip()
            return json.loads(cleaned)
    except:
        pass
        
    # Try finding first { definition
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            return json.loads(text[start : end + 1])
    except:
        pass
        
    return None



def get_meta_credentials(workspace_id, user_id):
    """
    Helper to retireve access_token and ad_account_id for a workspace/user pair.
    Returns (access_token, ad_account_id, error_response)
    """
    if not workspace_id:
        return None, None, (jsonify({"error": "workspace_id required"}), 400)
    
    # We allow user_id to be optional if the workspace logic handles it, 
    # but get_social_accounts_for_workspace_user expects it. 
    # If not provided, we might fail or need a fallback. 
    # For now, we'll assume it's passed or derived from session if needed, 
    # but the routes specs say query params.
    if not user_id:
        # Try to get from request header or something if needed, strictly spec says query param
        pass # Will pass None to helper

    accounts = get_social_accounts_for_workspace_user(workspace_id, user_id)
    if not accounts:
        return None, None, (jsonify({"error": "No linked Facebook account found for this workspace"}), 404)
    
    # Prefer an account with an ad_account_id already selected
    # If multiple, take the first one or logic to select? 
    # Spec doesn't specify selection logic, so taking the most recently updated (returned by query)
    account = accounts[0]
    access_token = account.get("access_token")
    
    # If ad_account_id is missing in DB, try to discover
    ad_account_id = account.get("ad_account_id")
    if not ad_account_id and access_token:
        discovered = discover_ad_accounts_for_token(access_token)
        if discovered:
            ad_account_id = discovered[0] # Pick first one
            
    if not access_token:
         return None, None, (jsonify({"error": "Access token missing for linked account"}), 500)

    # Ensure ad_account_id has 'act_' prefix if present
    if ad_account_id and not str(ad_account_id).startswith("act_"):
        ad_account_id = f"act_{ad_account_id}"

    return access_token, ad_account_id, None


# ----------------------------------------------------------------
# 1. Meta Infrastructure
# ----------------------------------------------------------------

@bp.route("/facebook/pages", methods=["GET", "OPTIONS"])
def list_pages():
    """
    Fetches pages the user has permission to advertise on.
    Query Params: workspace_id, user_id (optional)
    """
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id") # Optional by spec but needed for DB lookup usually
    
    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    url = f"{BASE_URL}/me/accounts"
    params = {
        "access_token": access_token,
        "fields": "id,name,access_token,tasks",
        "limit": 100
    }
    
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        if not resp.ok:
            logger.error(f"Meta Pages Error: {data}")
            return jsonify(data), resp.status_code
            
        # Parse and format
        pages = []
        for item in data.get("data", []):
            pages.append({
                "id": item.get("id"),
                "name": item.get("name"),
                "access_token": item.get("access_token") # Returning page token as per spec
            })
            
        return jsonify({"data": pages})
        
    except Exception as e:
        logger.exception("Failed to fetch pages")
        return jsonify({"error": str(e)}), 500


@bp.route("/meta/forms", methods=["GET", "OPTIONS"])
def list_lead_forms():
    """
    Fetches Lead Gen forms for the user's pages.
    Query Params: workspace_id, page_id (optional but recommended), user_id (optional)
    """
    workspace_id = request.args.get("workspace_id")
    page_id = request.args.get("page_id")
    user_id = request.args.get("user_id")
    
    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    pages_with_tokens = [] # List of (pid, page_token)

    if page_id:
        # Fetch specific page token
        p_url = f"{BASE_URL}/{page_id}"
        p_params = {"access_token": access_token, "fields": "access_token"}
        try:
            p_resp = requests.get(p_url, params=p_params)
            if p_resp.ok:
                data = p_resp.json()
                if "access_token" in data:
                    pages_with_tokens.append((page_id, data["access_token"]))
                else:
                    logger.warning(f"No page access token found for page {page_id}")
            else:
                 logger.error(f"Failed to fetch page details for {page_id}: {p_resp.text}")
        except Exception as e:
             logger.error(f"Error fetching page token for {page_id}: {e}")

    else:
        # Fetch all pages with tokens. We need 'access_token' field.
        p_url = f"{BASE_URL}/me/accounts"
        p_params = {"access_token": access_token, "fields": "id,access_token", "limit": 50}
        try:
             p_resp = requests.get(p_url, params=p_params)
             if p_resp.ok:
                 data = p_resp.json().get("data", [])
                 for p in data:
                     if "id" in p and "access_token" in p:
                         pages_with_tokens.append((p["id"], p["access_token"]))
        except Exception as e:
             logger.error(f"Error fetching pages: {e}")
    
    all_forms = []
    
    for pid, page_token in pages_with_tokens:
        url = f"{BASE_URL}/{pid}/leadgen_forms"
        params = {
             "access_token": page_token,
             "fields": "id,name,created_time,page_id",
             "limit": 100
        }
        try:
            resp = requests.get(url, params=params)
            if resp.ok:
                forms = resp.json().get("data", [])
                # Inject page_id if not in response
                for f in forms:
                    if "page_id" not in f: 
                        f["page_id"] = pid
                all_forms.extend(forms)
            else:
                logger.warning(f"Failed to fetch forms for page {pid}: {resp.text}")
        except Exception as e:
            logger.error(f"Error fetching forms for page {pid}: {e}")

    return jsonify({"data": all_forms})


@bp.route("/facebook/targeting/search", methods=["GET", "OPTIONS"])
def search_targeting():
    """
    Proxies Meta Marketing API search.
    Query Params: q, type, workspace_id, user_id (optional)
    """
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    q = request.args.get("q")
    type_ = request.args.get("type", "adinterest") # default to adinterest
    
    if not q:
        return jsonify({"error": "Query 'q' is required"}), 400

    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error
        
    url = f"{BASE_URL}/search"
    params = {
        "q": q,
        "type": type_,
        "access_token": access_token,
        "limit": 50
    }
    # Add other params like 'class', 'targeting_list' if needed? 
    # Spec implies generic proxy.
    
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        if not resp.ok:
            return jsonify(data), resp.status_code
        return jsonify(data)
    except Exception as e:
        logger.exception("Targeting search failed")
        return jsonify({"error": str(e)}), 500


@bp.route("/facebook/previews/generate", methods=["POST", "OPTIONS"])
def generate_preview():
    """
    Generates ad preview.
    Query Params: workspace_id, user_id
    Body: object_story_spec, ad_format
    """
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    
    access_token, ad_account_id, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error
        
    if not ad_account_id:
        return jsonify({"error": "No Ad Account selected/found"}), 400
        
    body = request.get_json() or {}
    object_story_spec = body.get("object_story_spec")
    ad_format = body.get("ad_format", "DESKTOP_FEED_STANDARD")
    
    if not object_story_spec:
        return jsonify({"error": "object_story_spec required"}), 400
        
    # Endpoint: /{ad_account_id}/generatepreviews
    url = f"{BASE_URL}/{ad_account_id}/generatepreviews"
    
    payload = {
        "object_story_spec": json.dumps(object_story_spec), # Must be JSON string
        "ad_format": ad_format,
        "access_token": access_token
    }
    
    try:
        resp = requests.get(url, params=payload) # NOTE: generatepreviews is GET usually? Spec said POST body. 
        # Actually Meta api says GET usually for this. But the spec says POST.
        # Checking docs: Graph API allows POST or GET. POST is safer for large payloads.
        # Implementation: Let's try POST.
        
        # Adjust payload for POST
        # For POST, we send data
        params = {"access_token": access_token}
        post_data = {
            "object_story_spec": json.dumps(object_story_spec) if isinstance(object_story_spec, dict) else object_story_spec,
            "ad_format": ad_format
        }
        
        resp = requests.post(url, params=params, data=post_data)
        
        # If POST fails, fallback to GET just in case (though POST is better)
        if not resp.ok and resp.status_code == 405: # Method not allowed
             payload["object_story_spec"] = post_data["object_story_spec"]
             resp = requests.get(url, params=payload)

        data = resp.json()
        if not resp.ok:
            logger.error(f"Preview Gen Error: {data}")
            return jsonify(data), resp.status_code
            
        return jsonify(data)
        
    except Exception as e:
        logger.exception("Preview generation failed")
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------------------------
# 2. Campaign Management
# ----------------------------------------------------------------
@bp.route("/adsets", methods=["POST", "OPTIONS"])
def create_adset():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    access_token, ad_account_id, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    if not ad_account_id:
        return jsonify({"error": "No Ad Account found"}), 400

    data = request.get_json() or {}

    # ===== 1. VALIDATION =====
    missing = []
    for f in ["campaign_id", "name", "billing_event", "optimization_goal"]:
        if not data.get(f):
            missing.append(f)

    if missing:
        return jsonify({
            "error": "Missing required fields",
            "fields": missing
        }), 400

    # ===== 2. NORMALIZATION =====
    normalized = normalize_adset_input(data)

    # ===== 3. BUILD META PAYLOAD =====
    meta_payload = build_meta_adset_payload(normalized, access_token)

    # ===== 4. CLEANUP INVALID DATA =====
    cleaned = cleanup_meta_adset_payload(meta_payload)

    # ===== 5. ENCODE FIELDS FOR META =====
    encoded = encode_meta_adset_payload(cleaned)

    # ===== DEBUG PRINT =====
    print("\n===== FINAL META ADSET PAYLOAD =====")
    for k, v in encoded.items():
        print(f"{k}: {v}")
    print("====================================\n")

    # ===== 6. EXECUTE CALL =====
    url = f"{BASE_URL}/{ad_account_id}/adsets"
    try:
        resp = requests.post(url, data=encoded)
        resp_json = resp.json()

        if not resp.ok:
            print("META ADSET ERROR:", json.dumps(resp_json, indent=2))
            return jsonify(resp_json), resp.status_code

        return jsonify(resp_json)

    except Exception as e:
        print("ADSET CREATE FAILED:", e)
        return jsonify({"error": str(e)}), 500


def normalize_adset_input(data):
    out = data.copy()

    if not out.get("name"):
        out["name"] = f"AdSet {datetime.utcnow().isoformat()}"

    # TIME
    now = datetime.utcnow()
    start_in_days = int(out.get("start_in_days") or 0)
    duration_days = int(out.get("duration_days") or 2)

    out["start_time"] = (now + timedelta(days=start_in_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["end_time"] = (now + timedelta(days=start_in_days + duration_days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # BUDGET
    if out.get("daily_budget"):
        out["daily_budget"] = str(int(float(out["daily_budget"]) * 100))

    # TARGETING AUTO-FIX
    tgt = out.get("targeting")
    if isinstance(tgt, dict):
        geo = tgt.get("geo_locations", {})
        if isinstance(geo, dict) and "location_types" not in geo:
            geo["location_types"] = ["home", "recent"]
            tgt["geo_locations"] = geo
        out["targeting"] = tgt

    # PROMOTED OBJECT CLEANING
    po = out.get("promoted_object")
    if isinstance(po, dict):
        # remove empty pixel_id
        if po.get("pixel_id") in ("", None):
            po.pop("pixel_id", None)
        # remove empty promoted_object entirely
        if not po:
            out.pop("promoted_object", None)

    return out

def build_meta_adset_payload(data, access_token):
    payload = {
        "name": data["name"],
        "campaign_id": data["campaign_id"],
        "optimization_goal": data["optimization_goal"],
        "billing_event": data["billing_event"],
        "start_time": data["start_time"],
        "end_time": data["end_time"],
        "status": data.get("status") or "PAUSED",
        "access_token": access_token,
        "bid_strategy": data.get("bid_strategy") or "LOWEST_COST_WITHOUT_CAP"
    }

    if data.get("daily_budget"):
        payload["daily_budget"] = data["daily_budget"]

    # === FIX 1: TARGETING CLEAN ===
    tgt = data.get("targeting")
    if isinstance(tgt, dict):
        # remove empty flexible_spec objects
        flex = tgt.get("flexible_spec")
        if isinstance(flex, list):
            filtered = []
            for block in flex:
                if any(v for v in block.values() if isinstance(v, list) and len(v) > 0):
                    filtered.append(block)
            if filtered:
                tgt["flexible_spec"] = filtered
            else:
                tgt.pop("flexible_spec", None)

        # remove empty custom audiences
        for key in ["custom_audiences", "excluded_custom_audiences"]:
            val = tgt.get(key)
            if isinstance(val, list) and len(val) == 0:
                tgt.pop(key, None)

        payload["targeting"] = tgt

    # === FIX 2: PROMOTED OBJECT ===
    po = data.get("promoted_object")
    if isinstance(po, dict) and po:
        payload["promoted_object"] = po

    # === FIX 3: REACH OBJECTIVE REQUIRES pacing_type ===
    if payload["optimization_goal"] == "REACH":
        payload["pacing_type"] = ["standard"]

    return payload



def cleanup_meta_adset_payload(payload):
    cleaned = {}
    for k, v in payload.items():
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        cleaned[k] = v
    return cleaned


def encode_meta_adset_payload(payload):
    encoded = {}
    for k, v in payload.items():
        if isinstance(v, (dict, list)):
            encoded[k] = json.dumps(v)   # JSON encode dicts & lists
        else:
            encoded[k] = v
    return encoded


def _ensure_creative_id(ad_account_id, access_token, creative_data):
    """
    Resolves a creative_id. If creative_data has an ID, return it.
    Otherwise creates an AdCreative (image only flow).
    """
    if not creative_data:
        return None, "No creative data provided"
        
    # Pass through existing IDs
    if "creative_id" in creative_data:
        return creative_data["creative_id"], None
    if "id" in creative_data:
        return creative_data["id"], None

    create_url = f"{BASE_URL}/{ad_account_id}/adcreatives"

    payload = creative_data.copy()
    payload["access_token"] = access_token

    # ---- Auto-fill name
    if "name" not in payload:
        payload["name"] = f"Creative {datetime.now().isoformat()}"

    # ---- Image Upload handling
    image_hash = payload.get("image_hash")
    image_base64 = payload.get("image_base64")
    video_id = payload.get("video_id")

    if not image_hash and not image_base64 and not video_id:
        return None, "image_hash or image_base64 is required (or video_id for video ads)"

    if not image_hash and image_base64:
        try:
            if image_base64.startswith("data:"):
                image_base64 = image_base64.split(",", 1)[1]
            image_base64 = image_base64.replace("\n", "").replace("\r", "").replace(" ", "")

            from test import upload_base64_image_to_meta
            image_hash = upload_base64_image_to_meta(
                image_base64,
                ad_account_id.replace("act_", ""),
                access_token,
                "v23.0"
            )
            print("[_ensure_creative_id] Uploaded base64 → image_hash:", image_hash)
        except Exception as e:
            return None, f"Failed to upload image: {str(e)}"

    # ---- Existing object_story_spec case
    if "object_story_spec" in payload:
        spec = payload.get("object_story_spec", {})
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except:
                return None, "object_story_spec must be valid JSON"

        link_data = spec.get("link_data", {})
        if image_hash and not link_data.get("image_hash"):
            link_data["image_hash"] = image_hash
            spec["link_data"] = link_data
            payload["object_story_spec"] = spec

        # Cleanup unused top-level keys
        for k in ["title", "body", "image_base64", "image_hash", "link_url", "page_id", "call_to_action"]:
            payload.pop(k, None)

    # ---- Build simplified object_story_spec
    else:
        page_id = payload.get("page_id")
        if not page_id:
            return None, "page_id is required for creative creation"

        link_url = payload.get("link_url") or "https://facebook.com"
        cta_type = payload.get("call_to_action_type")
        if not cta_type and isinstance(payload.get("call_to_action"), dict):
            cta_type = payload["call_to_action"].get("type")
        lead_form_id = payload.get("lead_form_id")

        # Build CTA value dict (shared by both image & video paths)
        cta_value = {}
        if link_url:
            cta_value["link"] = link_url
        if lead_form_id:
            cta_value["lead_gen_form_id"] = lead_form_id

        if video_id:
            # ---- VIDEO AD creative
            # NOTE: "link" is NOT a valid top-level field in video_data.
            # The link must go inside call_to_action.value.link instead.
            video_data = {
                "video_id": video_id,
                "message": payload.get("body", ""),
                "title": payload.get("title", ""),
                "link_description": payload.get("description", ""),
            }
            # Attach thumbnail if we have one
            if image_hash:
                video_data["image_hash"] = image_hash
            # Attach CTA — link goes inside call_to_action.value
            cta_type = cta_type or "LEARN_MORE"
            if cta_value:
                video_data["call_to_action"] = {
                    "type": cta_type,
                    "value": cta_value
                }
            payload["object_story_spec"] = {
                "page_id": page_id,
                "video_data": video_data
            }
            print(f"[_ensure_creative_id] Built VIDEO creative with video_id={video_id}, link={link_url}")
        else:
            # ---- IMAGE AD creative
            link_data = {
                "message": payload.get("body", ""),
                "link": link_url,
                "name": payload.get("title", ""),
                "image_hash": image_hash
            }
            if cta_type:
                link_data["call_to_action"] = {
                    "type": cta_type,
                    "value": cta_value or {"link": link_url}
                }
            elif payload.get("call_to_action"):
                link_data["call_to_action"] = payload.get("call_to_action")
            payload["object_story_spec"] = {
                "page_id": page_id,
                "link_data": link_data
            }

        # Cleanup simplified keys
        for k in ["title", "body", "image_base64", "image_hash", "link_url", "page_id", "call_to_action", "video_url", "thumbnail_base64", "thumbnail_url"]:
            payload.pop(k, None)

    # ---- Convert object_story_spec to JSON string (Meta required)
    if isinstance(payload.get("object_story_spec"), dict):
        payload["object_story_spec"] = json.dumps(payload["object_story_spec"])

    # ---- Remove invalid top-level fields (critical)
    INVALID_KEYS = [
        "description",
        "video_id",
        "call_to_action_type",
        "thumbnail_url",
        "thumbnail_base64",
        "lead_form_id",
        "call_to_action",
        "video_url",
        "format",
    ]
    for k in INVALID_KEYS:
        if k in payload:
            print("[_ensure_creative_id] Removing invalid field:", k)
            payload.pop(k, None)

    # ---- Debug print payload
    print("\n======= DEBUG CREATIVE PAYLOAD SENT TO META =======")
    print(json.dumps(payload, default=str, indent=2))
    print("===================================================\n")

    # ---- POST creative (use form-data NOT JSON)
    try:
        resp = requests.post(create_url, data=payload)
        data = resp.json()

        if resp.ok and "id" in data:
            print("[_ensure_creative_id] Created creative_id:", data["id"])
            return data["id"], None

        print("[_ensure_creative_id] Create AdCreative Failed:", data)
        return None, data

    except Exception as e:
        return None, str(e)


@bp.route("/ads", methods=["POST", "OPTIONS"])
def create_ad():
    """
    Creates a new Ad.
    Supports: single_image, single_video, carousel.
    """
    if request.method == "OPTIONS":
        return jsonify({}), 200

    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    access_token, ad_account_id, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error
    if not ad_account_id:
        return jsonify({"error": "No Ad Account found"}), 400

    data = request.get_json(silent=True) or {}
    creative_input = data.get("creative") or {}

    # ── Detect format ──
    active_format = data.get("format", "single_image")
    carousel_cards = creative_input.get("carousel_cards") or data.get("carousel_cards") or []
    is_carousel = (active_format == "carousel") or data.get("is_carousel") or (len(carousel_cards) >= 2)
    is_video = (active_format == "single_video") or bool(creative_input.get("video_id"))

    adset_id = data.get("adset_id")
    if not adset_id:
        return jsonify({"error": "adset_id required"}), 400

    ad_name = data.get("name") or f"Ad_{datetime.utcnow().isoformat()}"
    ad_status = data.get("status", "PAUSED").upper()
    message = creative_input.get("body") or data.get("message") or " "
    headline = creative_input.get("title") or creative_input.get("headline") or data.get("headline") or ""
    description_text = creative_input.get("description") or data.get("description") or ""
    link = creative_input.get("link_url") or data.get("link") or "https://sociovia.com/"
    cta_type = creative_input.get("call_to_action_type") or data.get("cta") or "LEARN_MORE"
    lead_form_id = creative_input.get("lead_form_id") or data.get("lead_form_id")

    # Strip act_ from ad_account_id for upload helpers (they add it themselves)
    raw_account_id = ad_account_id.replace("act_", "")

    # ── Resolve page_id ──
    page_id = data.get("page_id") or creative_input.get("page_id")
    if not page_id:
        try:
            pages_url = f"{BASE_URL}/me/accounts"
            pages_resp = requests.get(pages_url, params={"access_token": access_token, "fields": "id,name", "limit": 5}, timeout=15)
            pages_data = pages_resp.json().get("data", [])
            if pages_data:
                page_id = pages_data[0]["id"]
        except Exception as e:
            logger.warning(f"[create_ad] Failed to fetch pages: {e}")
    if not page_id:
        return jsonify({"error": "no_facebook_pages_found"}), 400

    # ── Sanitize CTA ──
    VALID_CTAS = {
        "LEARN_MORE", "SHOP_NOW", "SIGN_UP", "BOOK_TRAVEL", "CONTACT_US",
        "SUBSCRIBE", "DOWNLOAD", "GET_QUOTE", "APPLY_NOW", "BUY_NOW",
        "GET_OFFER", "ORDER_NOW", "SEND_WHATSAPP_MESSAGE", "BOOK_NOW",
        "WATCH_MORE", "MESSAGE_PAGE", "CALL_NOW",
    }
    safe_cta = cta_type.upper().replace(" ", "_") if cta_type else "LEARN_MORE"
    if safe_cta not in VALID_CTAS:
        safe_cta = "LEARN_MORE"

    try:
        # ═══════════════════════════════════════════════
        #  CAROUSEL CREATIVE
        # ═══════════════════════════════════════════════
        if is_carousel and len(carousel_cards) >= 2:
            from test import upload_base64_image_to_meta
            import re as _re
            from concurrent.futures import ThreadPoolExecutor, as_completed

            # Upload all card images concurrently to reduce total time
            def _upload_card_image(idx_card):
                idx, card = idx_card
                card_hash = card.get("image_hash")
                card_b64 = card.get("image_base64")
                if not card_hash and card_b64:
                    if card_b64.startswith("data:"):
                        card_b64 = card_b64.split(",", 1)[1]
                    card_b64 = _re.sub(r"\s+", "", card_b64)
                    card_hash = upload_base64_image_to_meta(
                        card_b64, raw_account_id, access_token, FB_API_VERSION
                    )
                return idx, card_hash

            # Run uploads in parallel (max 5 threads)
            card_hashes = {}
            upload_errors = []
            with ThreadPoolExecutor(max_workers=5) as executor:
                futures = {executor.submit(_upload_card_image, (i, card)): i for i, card in enumerate(carousel_cards)}
                for future in as_completed(futures):
                    i = futures[future]
                    try:
                        idx, card_hash = future.result()
                        card_hashes[idx] = card_hash
                    except Exception as e:
                        upload_errors.append((i, str(e)))

            if upload_errors:
                first_err = upload_errors[0]
                logger.error(f"[create_ad] Carousel card {first_err[0]+1} image upload failed: {first_err[1]}")
                return jsonify({"error": f"carousel_card_{first_err[0]+1}_image_upload_failed", "details": first_err[1]}), 400

            child_attachments = []
            for i, card in enumerate(carousel_cards):
                card_hash = card_hashes.get(i)
                if not card_hash:
                    return jsonify({"error": f"carousel_card_{i+1}_no_image"}), 400

                child = {
                    "link": card.get("link") or link,
                    "image_hash": card_hash,
                    "name": card.get("headline") or card.get("name") or f"Card {i+1}",
                }
                if card.get("description"):
                    child["description"] = card["description"]
                card_cta = safe_cta
                if lead_form_id:
                    child["call_to_action"] = {"type": card_cta, "value": {"lead_gen_form_id": str(lead_form_id)}}
                else:
                    child["call_to_action"] = {"type": card_cta}
                child_attachments.append(child)

            carousel_link_data = {
                "link": link,
                "message": message,
                "child_attachments": child_attachments,
                "multi_share_end_card": False,
            }
            if lead_form_id:
                carousel_link_data["call_to_action"] = {"type": safe_cta, "value": {"lead_gen_form_id": str(lead_form_id)}}

            creative_resp = requests.post(
                f"{BASE_URL}/{ad_account_id}/adcreatives",
                params={"access_token": access_token},
                data={
                    "name": f"Carousel-{ad_name}",
                    "object_story_spec": json.dumps({"page_id": page_id, "link_data": carousel_link_data}),
                },
                timeout=30
            ).json()

        # ═══════════════════════════════════════════════
        #  VIDEO CREATIVE
        # ═══════════════════════════════════════════════
        elif is_video:
            video_id = creative_input.get("video_id") or data.get("video_id")
            if not video_id:
                return jsonify({"error": "video_id required for video ads"}), 400

            image_hash = None
            thumb_b64 = creative_input.get("image_base64") or creative_input.get("thumbnail_base64")
            if thumb_b64:
                try:
                    if thumb_b64.startswith("data:"):
                        thumb_b64 = thumb_b64.split(",", 1)[1]
                    from test import normalize_image_for_meta, upload_image_to_meta
                    safe_bytes = normalize_image_for_meta(thumb_b64)
                    image_hash = upload_image_to_meta(safe_bytes, raw_account_id, access_token, FB_API_VERSION)
                except Exception as e:
                    logger.warning(f"[create_ad] Thumbnail upload failed: {e}")

            video_data = {
                "video_id": video_id,
                "message": message,
                "title": headline,
                "link_description": description_text,
            }
            if image_hash:
                video_data["image_hash"] = image_hash
            cta_value = {"link": link}
            if lead_form_id:
                cta_value["lead_gen_form_id"] = str(lead_form_id)
            video_data["call_to_action"] = {"type": safe_cta, "value": cta_value}

            creative_resp = requests.post(
                f"{BASE_URL}/{ad_account_id}/adcreatives",
                params={"access_token": access_token},
                data={
                    "name": f"Video-{ad_name}",
                    "object_story_spec": json.dumps({"page_id": page_id, "video_data": video_data}),
                },
                timeout=30
            ).json()

        # ═══════════════════════════════════════════════
        #  SINGLE IMAGE CREATIVE (original flow)
        # ═══════════════════════════════════════════════
        else:
            if not creative_input:
                return jsonify({"error": "Missing 'creative' field"}), 400

            # Merge root-level page_id
            if "page_id" in data and "page_id" not in creative_input:
                creative_input["page_id"] = data["page_id"]

            has_image = creative_input.get("image_hash") or creative_input.get("image_base64")
            has_vid = creative_input.get("video_id")
            if not has_image and not has_vid:
                return jsonify({
                    "error": "Creative must include image_hash or image_base64 (or video_id for video ads)",
                    "required_fields": ["image_hash", "image_base64", "video_id"]
                }), 400

            creative_id, cr_error = _ensure_creative_id(ad_account_id, access_token, creative_input)
            if cr_error:
                logger.error(f"[create_ad] Failed to create creative: {cr_error}")
                return jsonify({"error": "Failed to resolve creative", "details": cr_error}), 400

            # Create Ad directly using creative_id
            url = f"{BASE_URL}/{ad_account_id}/ads"
            ad_payload = {
                "name": ad_name,
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative_id}),
                "status": ad_status,
                "access_token": access_token,
            }
            resp = requests.post(url, data=ad_payload, timeout=30)
            resp_data = resp.json()
            if not resp.ok:
                logger.error(f"[create_ad] Create Ad Error: {resp_data}")
                return jsonify(resp_data), resp.status_code
            logger.info(f"[create_ad] Successfully created ad: {resp_data.get('id')}")
            return jsonify({"ok": True, "ad_id": resp_data.get("id"), "format": "single_image"})

        # ── Check creative creation result (carousel & video paths) ──
        if creative_resp.get("error"):
            logger.error(f"[create_ad] Creative creation failed: {creative_resp}")
            return jsonify({
                "ok": False, "stage": "creative",
                "error": creative_resp["error"].get("message", "Creative creation failed"),
                "details": creative_resp,
            }), 500
        creative_id = creative_resp["id"]

        # ── Create the Ad object (carousel & video paths) ──
        ad_resp = requests.post(
            f"{BASE_URL}/{ad_account_id}/ads",
            params={"access_token": access_token},
            data={
                "name": ad_name,
                "adset_id": adset_id,
                "creative": json.dumps({"creative_id": creative_id}),
                "status": ad_status,
            },
            timeout=30
        ).json()

        if ad_resp.get("error"):
            logger.error(f"[create_ad] Ad creation failed: {ad_resp}")
            return jsonify({
                "ok": False, "stage": "ad",
                "error": ad_resp["error"].get("message", "Ad creation failed"),
                "details": ad_resp,
            }), 500

        return jsonify({
            "ok": True,
            "ad_id": ad_resp["id"],
            "creative_id": creative_id,
            "format": "carousel" if is_carousel else "single_video" if is_video else "single_image",
        })

    except Exception as e:
        logger.exception("[create_ad] Ad creation failed")
        return jsonify({"error": str(e)}), 500


@bp.route("/adsets/<string:adset_id>", methods=["POST", "PATCH", "PUT", "OPTIONS"])
def update_adset(adset_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}
    
    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    url = f"{BASE_URL}/{adset_id}"
    
    # Meta Graph API updates are POST requests with fields
    # PATCH is often used for partial updates, mapping to POST in Meta
    
    payload = data.copy()
    payload["access_token"] = access_token
    
    # If it's a PATCH/PUT, we might need to handle specific fields or just pass through
    # For now, simplistic pass-through
    
    try:
        resp = requests.post(url, json=payload)
        resp_json = resp.json()
        
        if not resp.ok:
            return jsonify(resp_json), resp.status_code
            
        return jsonify(resp_json)
    except Exception as e:
        logger.exception("Update adset failed: %s", e)
        return jsonify({"error": str(e)}), 500


@bp.route("/ads/<string:ad_id>", methods=["POST", "PATCH", "PUT", "OPTIONS"])
def update_ad(ad_id: str):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    data = request.get_json(silent=True) or {}
    
    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    url = f"{BASE_URL}/{ad_id}"
    
    payload = data.copy()
    payload["access_token"] = access_token

    # Sanitize payload: remove image_url from link_data if present (causes 400)
    if "creative" in payload:
        creative = payload["creative"]
        if "object_story_spec" in creative:
            spec = creative["object_story_spec"]
            if "link_data" in spec:
                ld = spec["link_data"]
                if isinstance(ld, dict) and "image_url" in ld:
                     ld.pop("image_url", None)

    # Also check if it's sent directly (some frontends flatten it)
    if "object_story_spec" in payload:
         spec = payload["object_story_spec"]
         if "link_data" in spec:
             ld = spec["link_data"]
             if isinstance(ld, dict) and "image_url" in ld:
                 ld.pop("image_url", None)

    try:
        resp = requests.post(url, json=payload)
        resp_json = resp.json()
        
        if not resp.ok:
            return jsonify(resp_json), resp.status_code
            
        return jsonify(resp_json)
    except Exception as e:
        logger.exception("Update ad failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ----------------------------------------------------------------
# 3. AI Generation Routes
# ----------------------------------------------------------------

@bp.route("/ai/generate-ad-copy", methods=["POST", "OPTIONS"])
@bp.route("/v1/generate-copy", methods=["POST", "OPTIONS"])
def generate_copy():
    """
    Generate Meta Ad copy using Gemini (with optional product image analysis).
    - Strong image handling (download bytes + correct MIME)
    - Supports dict or string variations from the model
    - Returns structured JSON payload
    - SUPPORTS CAROUSEL ADS (is_carousel=True)
    """
    if not get_genai_client():
        return jsonify({"success": False, "error": "genai_client_not_initialized"}), 500

    body = request.get_json(silent=True) or {}
    product = body.get("product") or {}
    
    # ----------------------
    # CAROUSEL INPUTS
    # ----------------------
    is_carousel = body.get("is_carousel", False)
    card_count = body.get("card_count", 0)
    selected_images = body.get("selectedImages", [])
    if is_carousel:
        card_count = len(selected_images) if selected_images else (card_count or 2)

    # Count of variations to return
    try:
        count = max(1, int(body.get("count") or 3))
    except Exception:
        count = 3

    free_prompt = (body.get("prompt") or "").strip()

    # Resolve workspace
    workspace = None
    workspace_data = body.get("workspace")
    workspace_id = body.get("workspace_id") or body.get("workspaceId")

    if workspace_data:
         workspace = workspace_data
    elif workspace_id:
        try:
             # Try DB fetch directly using current_app to avoid loopback
             db = current_app.db
             # Need to import Workspace model here or use getattr pattern
             Workspace = getattr(current_app, "crm_models", {}).get("Workspace")
             
             if not Workspace: 
                  # Try pulling from models module if not in crm_models (fallback)
                  from models import Workspace as WS
                  Workspace = WS
                  
             if Workspace:
                  ws_obj = db.session.query(Workspace).filter_by(id=int(workspace_id)).first()
                  if ws_obj:
                       workspace = {
                            "name": ws_obj.business_name,
                            "business_name": ws_obj.business_name,
                            "website": ws_obj.website,
                            "description": ws_obj.description
                       }
        except Exception as e:
            logger.exception("fetch_workspace_failed_db: %s", e)
            workspace = None

    # Product text fields
    title = product.get("title") or product.get("brand") or ""
    short_desc = product.get("short_description") or product.get("description") or ""

    # Resolve image inputs for prompt
    image_parts = []
    
    if is_carousel:
        # Use first few images for context
        for img_obj in selected_images[:3]:
            url = img_obj.get("url")
            if url:
                try:
                    img_data, mime_type = _download_image_as_bytes(url)
                    if img_data:
                        image_parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=img_data)))
                except:
                    pass
    else:
        # Single Image Logic
        # Try both direct URL or nested object
        image_url = (
            body.get("imageUrl")
            or product.get("imageUrl")
            or (selected_images[0]["url"] if selected_images and isinstance(selected_images[0], dict) else None)
        )
        if image_url:
            try:
                img_data, mime_type = _download_image_as_bytes(image_url)
                if img_data:
                    image_parts.append(types.Part(inline_data=types.Blob(mime_type=mime_type, data=img_data)))
            except:
                pass


    # ----------------------
    # Prompt Construction
    # ----------------------
    prompt_lines = []
    
    if is_carousel:
        prompt_lines.append(f"TASK: Generate {count} high-quality Carousel Ad copy variations.")
        prompt_lines.append(f"Format: ONE JSON object with keys: success=true, variations=[...]")
        prompt_lines.append("Each variation MUST look like this:")
        prompt_lines.append("{")
        prompt_lines.append('  "id": "1",')
        prompt_lines.append('  "primary_text": "Shared ad text...",')
        prompt_lines.append('  "carousel_cards": [')
        prompt_lines.append(f'    // Generate exactly {card_count} cards')
        prompt_lines.append('    { "headline": "...", "description": "...", "cta": "SHOP_NOW" }')
        prompt_lines.append("  ]")
        prompt_lines.append("}")
    else:
        prompt_lines.append(f"TASK: Generate {count} high-quality Meta Ad copy variations.")
        prompt_lines.append("Output must strictly be ONE JSON object with keys:")
        prompt_lines.append("success=true, variations=[{id, primary_text, headline, description, cta, destination_url}]")

    prompt_lines.append("Tone: Premium, elegant, minimalist, global.")
    
    if title:
        prompt_lines.append(f"Product Title: {title}")
    if short_desc:
        prompt_lines.append(f"Description: {short_desc}")

    # Inject workspace branding
    if workspace:
        try:
            wname = workspace.get("name") or workspace.get("business_name") or ""
            wsite = workspace.get("website") or ""
            wdesc = workspace.get("description") or ""

            if wname:
                prompt_lines.append(f"Workspace Name: {wname}")
            if wsite:
                prompt_lines.append(f"Website: {wsite}")
            if wdesc:
                prompt_lines.append(f"Brand Description: {wdesc}")
        except Exception:
            pass

    if free_prompt:
        prompt_lines.append(f"Additional Instructions: {free_prompt}")
        
    prompt_lines.append("Return JSON ONLY. No markdown blocks.")

    final_prompt = "\n".join(prompt_lines)
    
    # ----------------------
    # Call Gemini
    # ----------------------
    contents = [final_prompt]
    if image_parts:
        contents.extend(image_parts)

    try:
        resp = _generate_content_robust(contents, temperature=GENAI_TEMPERATURE)

        # --- AI usage metering (fail-soft) ---
        try:
            from subscription.service import record_ai_usage, resolve_workspace_owner
            _meter_uid, _meter_wid = resolve_workspace_owner(workspace_id)
            record_ai_usage(
                _meter_uid,
                _meter_wid,
                feature="crm_content",
                model=GENAI_MODEL,
                route_path=request.path,
                _commit=True,
            )
        except Exception:
            pass
        # --- end metering ---

        raw_text = _extract_text_from_genai_response(resp)
        
        parsed = _extract_json_from_textt(raw_text)
        
        if not parsed:
            # Fallback if JSON fails but we have text
            parsed = {"success": False, "raw": raw_text}
        
        variations = parsed.get("variations", [])
        if not variations and isinstance(parsed, list):
            variations = parsed
            
        # ----------------------
        # Post-Process Logic
        # ----------------------
        result_payload = {
            "success": True,
            "is_carousel": is_carousel,
            "workspace": workspace or {},
            "variations": []
        }
        
        normalized_vars = []
        for i, item in enumerate(variations):
            # Normalization logic
            normalized = item.copy() if isinstance(item, dict) else {"primary_text": str(item)}
            if "id" not in normalized:
                normalized["id"] = str(i + 1)
            normalized_vars.append(normalized)
            
        result_payload["variations"] = normalized_vars

        if is_carousel:
            result_payload["card_count"] = card_count
            
            # Map images to first variation's cards (or separate 'carousel_cards' key as requested)
            if normalized_vars:
                best_var = normalized_vars[0]
                result_payload["primary_text"] = best_var.get("primary_text")
                
                # Build the enriched carousel_cards list for the top-level response
                cards_copy = best_var.get("carousel_cards", [])
                
                # Fill with image data
                enriched_cards = []
                for i in range(card_count):
                    card_data = cards_copy[i] if i < len(cards_copy) else {}
                    img_data = selected_images[i] if i < len(selected_images) else {}
                    
                    enriched_cards.append({
                        "card_index": i,
                        "image_id": img_data.get("id") if isinstance(img_data, dict) else None,
                        "image_url": img_data.get("url") if isinstance(img_data, dict) else None,
                        "headline": card_data.get("headline", ""),
                        "description": card_data.get("description", ""),
                        "link": card_data.get("link") or body.get("link") or (workspace.get("website") if workspace else "") or "",
                        "cta": card_data.get("cta", "LEARN_MORE")
                    })
                
                result_payload["carousel_cards"] = enriched_cards

        return jsonify(result_payload)

    except Exception as e:
        logger.exception("gemini_copy_failed")
        return jsonify({"success": False, "error": str(e)}), 500


@bp.route("/ai/generate-targeting", methods=["POST", "OPTIONS"])
def generate_targeting():
    """
    Suggests interest keywords using Google GenAI (Vertex AI).
    """
    client = get_genai_client()
    if not client:
        return jsonify({"error": "AI service not configured (Client init failed)"}), 503
        
    data = request.get_json() or {}
    description = data.get("description", "")
    context = data.get("context", {})
    
    prompt = f"""
    You are a Facebook Ads expert. Suggest effective targeting interests for this campaign.
    
    Product/Service: {description}
    Context: {json.dumps(context)}
    
    Provide a list of 10-15 detailed interest targeting keywords that would be available in Facebook Ads Manager.
    
    Output strictly in JSON format:
    {{
        "interests": [
            {{ "name": "Interest Name 1" }},
            {{ "name": "Interest Name 2" }}
        ]
    }}
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt
        )
        text = response.text

        # --- AI usage metering (fail-soft) ---
        # NOTE: this handler does not normally receive a workspace_id/user_id, so
        # unless the client sends one (body, context, or query string) this call
        # safely no-ops (record_ai_usage no-ops when user_id is falsy).
        try:
            from subscription.service import record_ai_usage, resolve_workspace_owner
            _meter_wsid = (
                data.get("workspace_id")
                or data.get("workspaceId")
                or (context.get("workspace_id") if isinstance(context, dict) else None)
                or request.args.get("workspace_id")
            )
            _meter_uid = (
                data.get("user_id")
                or (context.get("user_id") if isinstance(context, dict) else None)
                or request.args.get("user_id")
            )
            _meter_wid = _meter_wsid
            if not _meter_uid and _meter_wsid:
                _meter_uid, _meter_wid = resolve_workspace_owner(_meter_wsid)
            record_ai_usage(
                _meter_uid,
                _meter_wid,
                feature="crm_content",
                model="gemini-3.1-flash-lite",
                route_path=request.path,
                _commit=True,
            )
        except Exception:
            pass
        # --- end metering ---
        
        if "```json" in text:
             text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
             text = text.split("```")[1].strip()

        result = json.loads(text)
        # Add mock IDs as per spec requirement mostly for consistency
        for item in result.get("interests", []):
            if "id" not in item:
                item["id"] = "ai_suggested" # Placeholder
                
        return jsonify(result)
        
    except Exception as e:
        logger.exception("AI Targeting Generation failed")
        return jsonify({"error": str(e)}), 500

# ----------------------------------------------------------------
# 4. PERFORMANCE RECOMMENDATIONS (SUGGESTIONS)
# ----------------------------------------------------------------

def _fetch_recommendations(object_id, access_token):
    """
    Internal helper to fetch Performance Recommendations for Campaign/Adset/Ad.
    Returns a list of clean text suggestions.
    """
    url = f"{BASE_URL}/{object_id}/performance_recommendations"
    params = {"access_token": access_token}

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        if not resp.ok:
            return [], data  # suggestions=[], error=data

        suggestions = []

        for item in data.get("data", []):
            title = item.get("title")
            desc = item.get("description")

            if title:
                suggestions.append(title)
            elif desc:
                suggestions.append(desc)

        return suggestions, None

    except Exception as e:
        return [], {"error": str(e)}


@bp.route("/suggestions/campaign/<campaign_id>", methods=["GET", "OPTIONS"])
def get_campaign_suggestions(campaign_id):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    suggestions, meta_error = _fetch_recommendations(campaign_id, access_token)

    return jsonify({
        "level": "campaign",
        "id": campaign_id,
        "suggestions": suggestions,
        "meta_error": meta_error
    })


@bp.route("/suggestions/adset/<adset_id>", methods=["GET", "OPTIONS"])
def get_adset_suggestions(adset_id):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    suggestions, meta_error = _fetch_recommendations(adset_id, access_token)

    return jsonify({
        "level": "adset",
        "id": adset_id,
        "suggestions": suggestions,
        "meta_error": meta_error
    })


@bp.route("/suggestions/ad/<ad_id>", methods=["GET", "OPTIONS"])
def get_ad_suggestions(ad_id):
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")

    access_token, _, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    suggestions, meta_error = _fetch_recommendations(ad_id, access_token)

    return jsonify({
        "level": "ad",
        "id": ad_id,
        "suggestions": suggestions,
        "meta_error": meta_error
    })

def fetch_account_recommendations(ad_account_id, access_token):
    url = f"{BASE_URL}/{ad_account_id}/recommendations"
    params = {"access_token": access_token}

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if not resp.ok:
            return None, data
        
        rec_list = []
        for wrapper in data.get("data", []):
            for rec in wrapper.get("recommendations", []):
                rec_list.append(rec)

        return rec_list, None

    except Exception as e:
        return None, {"error": str(e)}

def classify_recommendations(recs):
    result = {
        "campaign": [],
        "adset": [],
        "ad": [],
        "unknown": []
    }

    for rec in recs:
        objs = rec.get("object_ids", [])
        rec_type = rec.get("type")
        content = rec.get("recommendation_content", {})
        body = content.get("body")
        lift = content.get("lift_estimate")
        score = content.get("opportunity_score_lift")
        url = rec.get("url")

        entry = {
            "type": rec_type,
            "body": body,
            "lift_estimate": lift,
            "opportunity_score_lift": score,
            "url": url,
            "object_ids": objs
        }

        # VERY SIMPLE HEURISTIC:
        # Campaign IDs are shorter, AdSet/Ads longer — Meta structure implies:
        # ⬇️ Feel free to replace with actual DB lookup if needed
        for oid in objs:
            if oid.startswith("120"): # campaigns usually start same prefix
                result["campaign"].append(entry)
            elif len(oid) > 15:
                result["ad"].append(entry)
            else:
                result["adset"].append(entry)

    return result

@bp.route("/meta/recommendations", methods=["GET", "OPTIONS"])
def get_meta_recommendations():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    
    access_token, ad_account_id, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error
        
    if not ad_account_id:
        return jsonify({"error": "No Ad Account found"}), 400

    recs, fetch_err = fetch_account_recommendations(ad_account_id, access_token)
    if fetch_err:
        return jsonify({
            "success": False,
            "error": fetch_err
        }), 400

    structured = classify_recommendations(recs)
    
    return jsonify({
        "success": True,
        "ad_account_id": ad_account_id,
        "counts": {
            "campaign": len(structured["campaign"]),
            "adset": len(structured["adset"]),
            "ad": len(structured["ad"]),
            "unknown": len(structured["unknown"]),
        },
        "recommendations": structured
    })

@bp.route("/meta/recommendations/apply", methods=["POST", "OPTIONS"])
def apply_recommendation():
    body = request.get_json() or {}
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    
    sig = body.get("recommendation_signature")
    params = body.get("params") or {}

    if not sig:
        return jsonify({"error": "recommendation_signature is required"}), 400

    access_token, ad_account_id, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error
        
    url = f"{BASE_URL}/{ad_account_id}/recommendations"
    payload = {
        "access_token": access_token,
        "recommendation_signature": sig
    }
    payload.update(params)

    resp = requests.post(url, data=payload)
    data = resp.json()
    
    if not resp.ok:
        return jsonify({"success": False, "error": data}), resp.status_code
    
    return jsonify({"success": True, "result": data})


META_INSIGHT_FIELDS = [
    "impressions",
    "reach",
    "clicks",
    "spend",
    "cpm",
    "cpc",
    "ctr",
    "actions",
    "action_values",
    "conversions",
    "conversion_values"
]

def fetch_meta_insights(ad_account_id, access_token, level, time_range=None):
    """
    Generic insights fetcher for campaign/adset/ad level metrics.
    """
    url = f"{BASE_URL}/{ad_account_id}/insights"

    params = {
        "access_token": access_token,
        "fields": ",".join(META_INSIGHT_FIELDS),
        "level": level,
        "time_increment": 1  # daily breakdown
    }

    # Optional: time_range
    if time_range:
        params["time_range"] = json.dumps(time_range)

    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()

        if not resp.ok:
            return None, data

        return data.get("data", []), None

    except Exception as e:
        return None, {"error": str(e)}


@bp.route("/meta/metrics", methods=["GET", "OPTIONS"])
def get_meta_metrics():
    workspace_id = request.args.get("workspace_id")
    user_id = request.args.get("user_id")
    level = request.args.get("level", "campaign")  # campaign | adset | ad
    since = request.args.get("since")
    until = request.args.get("until")

    if level not in ["campaign", "adset", "ad"]:
        return jsonify({"error": "Invalid level"}), 400

    access_token, ad_account_id, error = get_meta_credentials(workspace_id, user_id)
    if error:
        return error

    # Time range default to last 7 days
    time_range = None
    if since and until:
        time_range = {"since": since, "until": until}

    rows, fetch_err = fetch_meta_insights(
        ad_account_id, access_token, level, time_range
    )

    if fetch_err:
        return jsonify({"success": False, "error": fetch_err}), 400

    normalized = [normalize_insight_row(r) for r in rows]
    
    return jsonify({
        "success": True,
        "level": level,
        "ad_account_id": ad_account_id,
        "total_days": len(normalized),
        "metrics": normalized
    })

def normalize_insight_row(row):
    out = {
        "date": row.get("date_start"),
        "impressions": int(row.get("impressions", 0)),
        "reach": int(row.get("reach", 0)),
        "clicks": int(row.get("clicks", 0)),
        "spend": float(row.get("spend", 0)),
        "cpm": float(row.get("cpm", 0)),
        "cpc": float(row.get("cpc", 0)),
        "ctr": float(row.get("ctr", 0)),
        "conversions": 0,
        "conversion_value": 0
    }

    # Extract conversion actions
    for action in (row.get("actions") or []):
        if action.get("action_type") == "offsite_conversion":
            out["conversions"] += int(action.get("value", 0))

    for value in (row.get("action_values") or []):
        if value.get("action_type") == "offsite_conversion":
            out["conversion_value"] += float(value.get("value", 0))

    return out

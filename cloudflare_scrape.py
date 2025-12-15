import os
from cloudflare import Cloudflare
from datetime import datetime
from pymongo import MongoClient
from dotenv import load_dotenv
from urllib.parse import quote_plus
import time
from urllib.parse import urlparse
import json
import traceback
import pycountry

load_dotenv()
GITHUB_PAT = os.getenv("GITHUB_PAT")
MONGO_USER = os.getenv("MONGO_USER", "admin")
MONGO_USER = quote_plus(MONGO_USER)
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD", "password")
MONGO_PASSWORD =quote_plus(MONGO_PASSWORD)

MONGO_URI= str(f"mongodb://{MONGO_USER}:{MONGO_PASSWORD}@rack.rousoftware.com:27017/phishing_db?authSource=admin")
# url encode username and password


def get_db():
    client = MongoClient(MONGO_URI)
    return client.test_db

db = get_db()

collection = db.phishing_urls
client = Cloudflare(
    api_token="rAr-RWpixbTLqtwjxgOX8VxTf1dTuaEmxHL_CENV",  # This is the default and can be omitted
)
scans = client.url_scanner.scans.list(
    account_id="3befc94517c340c011b8628fdb363f4b",
)


def get_clean_country_code(raw_input):
    """
    Ensures we always return a 2-letter ISO code (e.g., 'US', 'LT').
    Handles cases where API gives 'United States' or 'Lithuania'.
    """
    if not raw_input:
        return None
        
    # 1. If it's already 2 letters (e.g. 'LT'), assume it's good
    if len(raw_input) == 2:
        return raw_input.upper()
        
    # 2. Try to look up by Name (e.g. "Lithuania" -> "LT")
    try:
        match = pycountry.countries.search_fuzzy(raw_input)
        if match:
            return match[0].alpha_2
    except LookupError:
        pass
        
    # 3. Fallback: If we can't find it, return the raw input or None
    return raw_input


for i in range(0, 100):
        latest = scans.results[i]
        scan_id = latest.api_id

        try:
            report = client.url_scanner.scans.get(
                account_id="3befc94517c340c011b8628fdb363f4b",
                scan_id=scan_id
            )

            raw_url = report.page.url
            parsed = urlparse(raw_url)
            host_val = parsed.netloc

            if host_val and '.' in host_val:
                tld_val = host_val.split('.')[-1]
            else:
                tld_val = None

            cert_issued_to = None
            cert_issued_by = None
            cert_serial = None
            if hasattr(report, 'lists') and report.lists.certificates:
                main_cert = report.lists.certificates[0]
                
                cert_issued_to = getattr(main_cert, 'subject', None) or getattr(main_cert, 'issuer', None)
                cert_issued_by = getattr(main_cert, 'issuer', None)
                cert_serial = getattr(main_cert, 'serialNumber', None)

            asn_name_val = getattr(report.page, 'asnname', None)

            mongo_document = {
                "_id": report.task.uuid,
                "url": report.page.url,
                "ip": report.page.ip,
                "asn": report.page.asn,
                "country_code": get_clean_country_code(report.page.country),
                "isotime": report.task.time,
                "country_name": report.page.country, 
                #"malicious": report.verdicts.malicious, olmadi, olsa da hepsi false gelecek zatem
                "host": host_val,
                "tld": tld_val,
                "added_at": time.time(),
                "ssl_cert_issued_to": cert_issued_to,
                "asn_name": asn_name_val,
                "ssl_cert_issued_by": cert_issued_by,
                "ssl_cert_serial": cert_serial,
                "visited": None,
                "error": None,
                "brand": None,
                "family_id": None,
                "is_spear": None,
                "sector": None,
                "discover_time": None,
                "page_language": None,
                "visited_at": None
            }
            
            collection.update_one(
                {"_id": mongo_document["_id"]}, 
                {"$set": mongo_document}, 
                upsert=True
            )
            
            print(f"[{i+1}/100] Saved {scan_id} ")
            

        except Exception as e:
            print(f"Error on index {i}: {e}")

 
        time.sleep(1) 


def get_malicious_status(report_obj):
    """Safely extracts the malicious verdict from a slippery object."""
    verdicts = getattr(report_obj, 'verdicts', None)
    if not verdicts:
        return False # Default to safe if missing

    # Attempt 1: Standard Dictionary Access (Most likely fix)
    # The object might require ['key'] instead of .key
    try:
        return verdicts['malicious']
    except (TypeError, KeyError, AttributeError):
        pass

    # Attempt 2: Direct Attribute
    try:
        return verdicts.malicious
    except AttributeError:
        pass
        
    # Attempt 3: .get() method
    if hasattr(verdicts, 'get'):
        return verdicts.get('malicious', False)

    # Attempt 4: to_dict() method (Common in generated SDKs)
    if hasattr(verdicts, 'to_dict'):
        return verdicts.to_dict().get('malicious', False)

    return False # Give up, assume Safe

#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlparse

PRICE_HOST = "https://pricing.us-east-1.amazonaws.com"
SERVICE_INDEX_URL = PRICE_HOST + "/offers/v1.0/aws/index.json"
MANIFEST_KEY = "manifests/price-list-manifest.json"
LOCAL_MANIFEST = "price-list-manifest.json"
CHUNK_SIZE = 1024 * 1024
DEFAULT_MAX_FILE_MB = 100
RETRY_BACKOFF_SECONDS = 2

SERVICE_CODES = ["AmazonS3", "AWSLambda", "AmazonDynamoDB", "AmazonApiGateway", "AmazonCognito"]
REGION_CODES = ["us-east-1", "ap-southeast-1", "ap-northeast-1", "ap-southeast-2", "ap-south-1", "ap-east-1", "eu-west-1"]

class SizeLimitExceeded(Exception):
    """Signal that a source crossed the selected byte limit."""


def retry_once(operation):
    # Give transient index and transfer failures one recovery attempt.
    for attempt in range(2):
        try:
            return operation()
        except SizeLimitExceeded:
            raise
        except Exception:
            if attempt == 1:
                raise
            time.sleep(RETRY_BACKOFF_SECONDS)


def load_json_url(url):
    # Close each index response after decoding its JSON document.
    with urllib.request.urlopen(url, timeout=60) as response:
        return json.load(response)
        
def base_entry(service, region, status, reason, source_url=None, key=None):
    # Give every requested pair the same auditable field set.
    parts = [] if source_url is None else urlparse(source_url).path.strip("/").split("/")
    version = parts[parts.index(service) + 1] if service in parts else None
    return {"serviceCode": service, "regionCode": region, "status": status, "reason": reason, "sourceUrl": source_url, "priceListVersion": version, "s3Key": key, "bytes": None, "sha256": None, "sourceRegionIndexPublicationDate": None}


def put_object(path, bucket, key, content_type):
    # PutObject stores a complete staged file at the selected key.
    command = ["aws", "s3api", "put-object", "--bucket", bucket, "--key", key, "--body", path, "--content-type", content_type]
    subprocess.run(command, stdout=subprocess.DEVNULL, check=True)

def stage_source(source_url, max_bytes):
    # Hash accepted bytes while keeping the temporary file bounded.
    digest = hashlib.sha256()
    total_bytes = 0
    staged = tempfile.NamedTemporaryFile(prefix="price-list-", suffix=".csv", delete=False)
    staged_path = staged.name
    try:
        with staged, urllib.request.urlopen(source_url, timeout=60) as response:
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                if total_bytes + len(chunk) > max_bytes:
                    raise SizeLimitExceeded("Source exceeded the {} byte limit".format(max_bytes))
                staged.write(chunk)
                digest.update(chunk)
                total_bytes += len(chunk)
    except Exception:
        if os.path.exists(staged_path):
            os.remove(staged_path)
        raise
    return staged_path, total_bytes, digest.hexdigest()
    
    
def ingest_pair(service, region, region_index, bucket, max_bytes):
    # Resolve one requested pair before attempting a bounded transfer.
    regions = region_index.get("regions", {})
    if region not in regions:
        entry = base_entry(service, region, "unavailable", "Region is absent from the service's current region index")
        entry["sourceRegionIndexPublicationDate"] = region_index.get("publicationDate")
        return entry
    relative_url = regions[region]["currentVersionUrl"]
    source_url = PRICE_HOST + relative_url[:-5] + ".csv"
    key = "raw/service={}/region={}/index.csv".format(service, region)
    parts = urlparse(source_url).path.strip("/").split("/")
    version = parts[parts.index(service) + 1]
    staged_path = None
    try:
        staged_path, byte_count, sha256_hex = retry_once(lambda: stage_source(source_url, max_bytes))
        retry_once(lambda: put_object(staged_path, bucket, key, "text/csv"))
    except SizeLimitExceeded as error:
        entry = base_entry(service, region, "size_limit_exceeded", str(error), source_url, key)
    except Exception as error:
        entry = base_entry(service, region, "error", "Transfer failed after two attempts: {}".format(error), source_url, key)
    else:
        entry = base_entry(service, region, "landed", None, source_url, key)
        entry.update({"bytes": byte_count, "sha256": sha256_hex, "priceListVersion": version})
        print("LANDED service={} region={} bytes={} sha256={}".format(service, region, byte_count, sha256_hex))
    finally:
        if staged_path and os.path.exists(staged_path):
            os.remove(staged_path)
    entry["sourceRegionIndexPublicationDate"] = region_index.get("publicationDate")
    return entry
    
def build_entries(services, regions, bucket, max_bytes):
    # Keep every requested pair in the manifest even when an index fails.
    try:
        service_index = retry_once(lambda: load_json_url(SERVICE_INDEX_URL))
    except Exception as error:
        reason = "Service index failed after two attempts: {}".format(error)
        entries = [base_entry(service, region, "error", reason) for service in services for region in regions]
        return entries, None
    offers = service_index.get("offers", {})
    entries = []
    for service in services:
        offer = offers.get(service)
        if offer is None:
            entries.extend(base_entry(service, region, "unavailable", "Service code is absent from the current AWS service index") for region in regions)
            continue
        try:
            region_index_url = PRICE_HOST + offer["currentRegionIndexUrl"]
            region_index = retry_once(lambda: load_json_url(region_index_url))
        except Exception as error:
            reason = "Region index failed after two attempts: {}".format(error)
            entries.extend(base_entry(service, region, "error", reason) for region in regions)
            continue
        for region in regions:
            try:
                entry = ingest_pair(service, region, region_index, bucket, max_bytes)
            except Exception as error:
                entry = base_entry(service, region, "error", "Regional index entry failed: {}".format(error))
            entries.append(entry)
    return entries, service_index
    
def persist_manifest(manifest, bucket):
    # Keep a local audit record even when both S3 attempts fail.
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    with open(LOCAL_MANIFEST, "wb") as manifest_file:
        manifest_file.write(manifest_bytes)
    try:
        retry_once(lambda: put_object(LOCAL_MANIFEST, bucket, MANIFEST_KEY, "application/json"))
    except Exception as error:
        print("MANIFEST ERROR local={} upload_failed_after_two_attempts={}".format(LOCAL_MANIFEST, error))
    else:
        print("MANIFEST s3://{}/{}".format(bucket, MANIFEST_KEY))
        
def main():
    # Parse the run mode and selected maximum source size.
    parser = argparse.ArgumentParser(description="Land regional AWS Price List CSV files with a complete manifest.")
    parser.add_argument("--bucket", required=True, help="Destination S3 bucket name")
    parser.add_argument("--sample", action="store_true", help="Attempt only AmazonS3 in us-east-1")
    parser.add_argument("--max-file-mb", type=int, default=DEFAULT_MAX_FILE_MB, help="Maximum accepted source size in MiB")
    args = parser.parse_args()
    services = SERVICE_CODES[:1] if args.sample else SERVICE_CODES
    regions = REGION_CODES[:1] if args.sample else REGION_CODES
    max_bytes = args.max_file_mb * 1024 * 1024
    try:
        entries, service_index = build_entries(services, regions, args.bucket, max_bytes)
    except Exception as error:
        reason = "Unexpected ingestion failure: {}".format(error)
        entries = [base_entry(service, region, "error", reason) for service in services for region in regions]
        service_index = None
    counts = {status: sum(1 for entry in entries if entry["status"] == status) for status in ("landed", "unavailable", "error", "size_limit_exceeded")}
    print("SUMMARY landed={landed} unavailable={unavailable} error={error} size_limit_exceeded={size_limit_exceeded}".format(**counts))
    manifest = {"generatedAtUtc": datetime.now(timezone.utc).isoformat(), "sourceServiceIndex": SERVICE_INDEX_URL, "sourceServiceIndexPublicationDate": service_index.get("publicationDate") if service_index else None, "services": SERVICE_CODES, "regions": REGION_CODES, "sampleMode": args.sample, "maxFileMb": args.max_file_mb, "entries": entries}
    persist_manifest(manifest, args.bucket)


if __name__ == "__main__":
    main()
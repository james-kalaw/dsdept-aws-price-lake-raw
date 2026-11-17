#!/usr/bin/env python3

import argparse
import hashlib
import json
import subprocess

MANIFEST_KEY = "manifests/price-list-manifest.json"
CHUNK_SIZE = 1024 * 1024


def download_bytes(s3_uri):
    result = subprocess.run(
        ["aws", "s3", "cp", s3_uri, "-", "--only-show-errors"],
        stdout=subprocess.PIPE,
        check=True,
    )
    return result.stdout

def stream_s3_hash(s3_uri):
    digest = hashlib.sha256()
    total_bytes = 0
    process = subprocess.Popen(
        ["aws", "s3", "cp", s3_uri, "-", "--only-show-errors"],
        stdout=subprocess.PIPE,
    )

    while True:
        chunk = process.stdout.read(CHUNK_SIZE)
        if not chunk:
            break
        digest.update(chunk)
        total_bytes += len(chunk)

    process.stdout.close()
    return_code = process.wait()
    if return_code != 0:
        raise RuntimeError("AWS CLI download failed for {}".format(s3_uri))

    return total_bytes, digest.hexdigest()

def main():
    parser = argparse.ArgumentParser(
        description="Re-stream S3 objects and compare them with the ingestion manifest."
    )
    parser.add_argument("--bucket", required=True, help="Raw-zone S3 bucket name")
    args = parser.parse_args()

    manifest_uri = "s3://{}/{}".format(args.bucket, MANIFEST_KEY)
    manifest = json.loads(download_bytes(manifest_uri))
    landed_entries = [
        entry for entry in manifest["entries"] if entry["status"] == "landed"
    ]
    
    failures = 0
    for entry in landed_entries:
        s3_uri = "s3://{}/{}".format(args.bucket, entry["s3Key"])
        try:
            byte_count, sha256_hex = stream_s3_hash(s3_uri)
            passed = (
                byte_count == entry["bytes"]
                and sha256_hex == entry["sha256"]
            )
        except RuntimeError as error:
            passed = False
            byte_count = 0
            sha256_hex = str(error)

        status = "PASS" if passed else "FAIL"
        print(
            "{} service={} region={} bytes={} sha256={}".format(
                status,
                entry["serviceCode"],
                entry["regionCode"],
                byte_count,
                sha256_hex,
            )
        )
        if not passed:
            failures += 1

    print(
        "VERIFY SUMMARY checked={} failures={}".format(
            len(landed_entries),
            failures,
        )
    )

    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()


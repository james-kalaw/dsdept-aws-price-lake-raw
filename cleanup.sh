#!/usr/bin/env bash
set -euo pipefail

STACK_NAME="price-lake-raw"
REGION="us-east-1"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
BUCKET="aws-price-lake-raw-${ACCOUNT_ID}-${REGION}"

if [[ "${1:-}" != "--confirm" ]]; then
  echo "Cleanup dry run for s3://${BUCKET}"
  aws s3 ls "s3://${BUCKET}" --recursive --human-readable --summarize
  echo "No resources were deleted."
  echo "Run: bash cleanup.sh --confirm"
  exit 0
fi

aws s3 rm "s3://${BUCKET}" --recursive
aws cloudformation delete-stack \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"
aws cloudformation wait stack-delete-complete \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}"

echo "Deleted stack ${STACK_NAME} and emptied bucket ${BUCKET}."


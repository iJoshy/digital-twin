#!/bin/bash

ensure_terraform_state_bucket() {
  local state_bucket="$1"
  local aws_region="$2"

  if aws s3api head-bucket --bucket "$state_bucket" 2>/dev/null; then
    echo "✅ Terraform state bucket exists: $state_bucket"
  else
    echo "🪣 Creating Terraform state bucket: $state_bucket"
    if [ "$aws_region" = "us-east-1" ]; then
      aws s3api create-bucket \
        --bucket "$state_bucket" \
        --region "$aws_region"
    else
      aws s3api create-bucket \
        --bucket "$state_bucket" \
        --region "$aws_region" \
        --create-bucket-configuration "LocationConstraint=$aws_region"
    fi
  fi

  aws s3api put-public-access-block \
    --bucket "$state_bucket" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

  aws s3api put-bucket-versioning \
    --bucket "$state_bucket" \
    --versioning-configuration Status=Enabled

  aws s3api put-bucket-encryption \
    --bucket "$state_bucket" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
}

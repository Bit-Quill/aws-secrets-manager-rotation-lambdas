#!/bin/bash
#
# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Ensure that pip3 is installed in the local environment
if ! command -v pip3 &> /dev/null
then
    echo "pip3 could not be found, please install it and re-run this script."
    exit 1
fi

# Make a temporary directory and populate with dependencies
mkdir tmp-influxdb-deployment-lambda
cd tmp-influxdb-deployment-lambda
pip3 install -r ../requirements.txt -t .

# Copy the lambda function code and create a zip of lambda with dependencies
cp ../lambda_function.py ./

zip -r ../influxdb-token-rotation-lambda.zip .

# Cleanup
cd ../
rm -rf tmp-influxdb-deployment-lambda

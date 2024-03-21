# Copyright 2023 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# Ensure that pip3 is installed in the local environment
if (-Not (Get-Command "pip3" -errorAction SilentlyContinue))
{
    Write-Output "pip3 could not be found, please install it and re-run this script."
    exit
}

# Make a temporary directory and populate with dependencies
mkdir tmp-influxdb-deployment-lambda
cd tmp-influxdb-deployment-lambda
pip3 install -r ..\requirements.txt -t . --no-user

# Copy the lambda function code and create a zip of lambda with dependencies
cp ..\lambda_function.py .\
Compress-Archive -Path * -DestinationPath ..\influxdb-token-rotation-lambda.zip -CompressionLevel Optimal -Force

# Cleanup
cd ..\
Remove-Item -Path .\tmp-influxdb-deployment-lambda -Force -Recurse

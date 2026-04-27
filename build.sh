#!/bin/bash
set -e

echo "=== Installing system dependencies for Playwright ==="

# Update package list
apt-get update

# Install Playwright browser dependencies
apt-get install -y \
    libglib2.0-0 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libatspi2.0-0 \
    libxinerama1 \
    libxi6 \
    libxtst6 \
    libnss3 \
    libxss1 \
    libasound2 \
    libexpat1 \
    libfontconfig1 \
    fonts-dejavu-core \
    libfreetype6 \
    libssl3 \
    libharfbuzz0b \
    libfribidi0 \
    libgraphite2-3

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Installing Playwright browsers ==="
playwright install chromium

echo "=== Build complete ==="

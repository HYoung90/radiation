#!/usr/bin/env bash
set -e

# 👉 여기에 실제 URI를 직접 넣습니다
export MONGO_URI="mongodb://mongo:ntbbrW…@gondola.proxy.rlwy.net:41228/Data?authSource=admin"

MONGO_LOCAL="mongodb://localhost:27017/Data"

for C in \
  Busan_radiation \
  Busan_radiation_backup \
  NPP_weather \
  NPP_weather_backup \
  nuclear_radiation \
  nuclear_radiation_backup \
  radiation_stats; do

  echo "▶ Exporting $C"
  mongoexport \
    --uri="$MONGO_LOCAL" \
    --collection="$C" \
    --out="$C.json"

  echo "▶ Importing $C"
  mongoimport \
    --uri="$MONGO_URI" \
    --db=Data \
    --collection="$C" \
    --file="$C.json" \
    --mode=upsert
done

echo "✅ All collections imported!"

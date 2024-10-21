# average_count.py
# 이 스크립트는 radiation_stats 컬렉션의 전제 데이터를 가져와
# 각 지역별로 비가 온 날과 비가 오지 않은 날을 카운트하고,
# 각각의 평균 방사선량과 방사선량 증가율을 콘솔에 출력합니다.

from pymongo import MongoClient
import os
from dotenv import load_dotenv
import sys

def get_mongo_connection():
    """
    MongoDB에 연결하고 클라이언트를 반환합니다.
    연결에 실패하면 스크립트를 종료합니다.
    """
    try:
        client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017/"))
        print("MongoDB 연결 성공.")
        return client
    except Exception as e:
        print(f"MongoDB 연결 실패: {e}")
        sys.exit(1)

def calculate_radiation_averages():
    """
    radiation_stats 컬렉션의 모든 데이터를 가져와
    각 지역별로 비가 온 날과 비가 오지 않은 날을 카운트하고,
    각각의 평균 방사선량과 방사선량 증가율을 계산하여 콘솔에 출력합니다.
    """
    client = get_mongo_connection()

    # 데이터베이스 및 컬렉션 설정
    db = client['power_plant_weather']
    stats_collection = db['radiation_stats']

    # 모든 데이터 가져오기
    data = list(stats_collection.find({}, {"_id": 0}))
    print(f"Fetched {len(data)} records from radiation_stats 컬렉션.\n")

    if not data:
        print("radiation_stats 컬렉션에 데이터가 없습니다.")
        return

    # rain 필드의 고유 값 확인
    unique_rain_values = set(entry.get('rain') for entry in data)
    print(f"Unique rain values: {unique_rain_values}\n")

    # 강우 여부에 따라 그룹핑 및 카운팅
    grouped_data = {}
    for entry in data:
        region = entry.get('region')
        rain = entry.get('rain')
        value = entry.get('value')

        if not region or value is None:
            print(f"유효하지 않은 데이터 항목 스킵: {entry}")
            continue

        if region not in grouped_data:
            grouped_data[region] = {
                'rain_days': 0,
                'no_rain_days': 0,
                'rain_values': [],
                'no_rain_values': []
            }

        # rain 필드의 값에 따라 강우 여부 결정
        if isinstance(rain, str):
            rain_lower = rain.lower()
            if rain_lower == 'rain':
                grouped_data[region]['rain_days'] += 1
                grouped_data[region]['rain_values'].append(float(value))
            elif rain_lower == 'no_rain':
                grouped_data[region]['no_rain_days'] += 1
                grouped_data[region]['no_rain_values'].append(float(value))
            else:
                print(f"알 수 없는 rain 상태 스킵: {entry}")
        elif isinstance(rain, bool):
            if rain:
                grouped_data[region]['rain_days'] += 1
                grouped_data[region]['rain_values'].append(float(value))
            else:
                grouped_data[region]['no_rain_days'] += 1
                grouped_data[region]['no_rain_values'].append(float(value))
        else:
            try:
                rain_status = float(rain) > 0
                if rain_status:
                    grouped_data[region]['rain_days'] += 1
                    grouped_data[region]['rain_values'].append(float(value))
                else:
                    grouped_data[region]['no_rain_days'] += 1
                    grouped_data[region]['no_rain_values'].append(float(value))
            except (TypeError, ValueError):
                print(f"비정상적인 rain 값 스킵: {entry}")
                continue

    # 평균값 및 증가 비율 계산
    results = []
    for region, values in grouped_data.items():
        rain_avg = (sum(values['rain_values']) / len(values['rain_values'])) if values['rain_values'] else 0.0
        no_rain_avg = (sum(values['no_rain_values']) / len(values['no_rain_values'])) if values['no_rain_values'] else 0.0

        if no_rain_avg > 0:
            percentage_increase = ((rain_avg - no_rain_avg) / no_rain_avg) * 100
            percentage_increase = round(percentage_increase, 2)
        else:
            percentage_increase = 'N/A'

        results.append({
            "region": region,
            "rain_days": values['rain_days'],
            "no_rain_days": values['no_rain_days'],
            "rain_avg": round(rain_avg, 3),
            "no_rain_avg": round(no_rain_avg, 3),
            "percentage_increase": percentage_increase
        })

    # 결과를 콘솔에 출력
    print("**Daily Radiation Averages Report**\n")
    for entry in results:
        print(f"Region: {entry['region']}")
        print(f"  Rain Days: {entry['rain_days']}")
        print(f"  No Rain Days: {entry['no_rain_days']}")
        print(f"  Rain Avg: {entry['rain_avg']} μSv/h")
        print(f"  No Rain Avg: {entry['no_rain_avg']} μSv/h")
        print(f"  Percentage Increase: {entry['percentage_increase']}%\n")

    print("Manual processing 완료.")

def main():
    calculate_radiation_averages()

if __name__ == "__main__":
    print("Radiation averages manual processing started.\n")
    try:
        main()
    except Exception as e:
        print(f"수동 처리 중 오류 발생: {e}")
        sys.exit(1)

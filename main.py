# main.py
import schedule
import time
import logging
from datetime import datetime
import sys
import threading # 스케줄러를 별도의 스레드에서 실행하기 위함

# 각 스케줄러 스크립트를 임포트합니다.
# 이 스크립트들이 같은 디렉토리에 있다고 가정합니다.
import NPP_weather
import NPP_radiation
import Busan_radiation
import data
import average
import Busan_alert

# 로깅 설정 (main.py의 로그도 볼 수 있도록)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(message)s',
    handlers=[
        logging.FileHandler('main_scheduler.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

def run_scheduler_in_thread(scheduler_name, scheduler_module):
    """각 스케줄러 모듈의 run_pending()을 별도의 스레드에서 실행합니다."""
    logging.info(f"[{scheduler_name}] 스케줄러 스레드 시작.")
    while True:
        scheduler_module.schedule.run_pending()
        time.sleep(1) # 1초 대기

if __name__ == "__main__":
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"main.py 스크립트 시작 (모든 스케줄러 통합) - 현재 시간: {current_time}")
    print(f"main.py 스크립트 시작 (모든 스케줄러 통합) - 현재 시간: {current_time}")

    # 각 스크립트의 초기 실행 함수를 호출합니다.
    # 이 함수들은 스크립트 시작 시 한 번 실행되어야 하는 로직을 포함합니다.
    logging.info("모든 스케줄러 스크립트의 초기 작업 실행 중...")
    NPP_weather.scheduled_task() # NPP_weather의 초기 실행 함수
    NPP_radiation.scheduled_task() # NPP_radiation의 초기 실행 함수
    Busan_radiation.scheduled_task() # Busan_radiation의 초기 실행 함수
    data.automated_process() # data.py의 초기 실행 함수
    average.automate() # average.py의 초기 실행 함수
    Busan_alert.scheduled_alert_task() # Busan_alert의 초기 실행 함수
    logging.info("모든 스케줄러 스크립트 초기 작업 완료.")


    # 각 스케줄러의 run_pending()을 별도의 스레드에서 실행
    # 이렇게 하면 한 스케줄러가 블로킹되어도 다른 스케줄러가 계속 작동합니다.
    scheduler_threads = []
    threads_to_run = [
        ("NPP_weather", NPP_weather),
        ("NPP_radiation", NPP_radiation),
        ("Busan_radiation", Busan_radiation),
        ("data", data),
        ("average", average),
        ("Busan_alert", Busan_alert)
    ]

    for name, module in threads_to_run:
        thread = threading.Thread(target=run_scheduler_in_thread, args=(name, module), daemon=True)
        scheduler_threads.append(thread)
        thread.start()

    # 메인 스레드는 모든 스케줄러 스레드가 계속 실행되도록 유지
    try:
        while True:
            time.sleep(60) # 메인 스레드는 1분마다 깨어나서 스레드들이 살아있는지 확인
    except KeyboardInterrupt:
        logging.info("스크립트 종료 요청 감지. 모든 스케줄러 스레드 종료 중...")
        print("스크립트 종료 요청 감지. 모든 스케줄러 스레드 종료 중...")
    finally:
        # atexit에 등록된 함수들이 호출될 것입니다.
        logging.info("main.py 스크립트 종료.")
        print("main.py 스크립트 종료.")
        sys.exit(0) # 정상 종료
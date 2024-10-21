# sepctrum.py
# 이 스크립트는 CSV 파일에서 방사선 스펙트럼 데이터를 읽고, 피크 탐지를 통해 감마선 방출을 분석합니다.
# 탐지된 피크를 알려진 핵종 리스트와 비교하여 가능성 있는 핵종을 식별합니다.


import pandas as pd
import numpy as np
from scipy.signal import find_peaks

# CSV 파일을 로컬 경로에서 불러오기
file_path = r'C:\Jupyter notebook\spec_test1.csv'
spect_data = pd.read_csv(file_path)

# 3 MeV를 1024 채널로 나누어 각 채널의 에너지 값을 계산
max_energy = 3  # MeV
num_channels = 1024
channel_width = max_energy / num_channels
spect_data['energy'] = spect_data['Channel'] * channel_width

# 피크 탐지 (감마선 방출 피크를 찾기 위해 count 값이 일정 수준 이상인 것들만 선택)
peaks, _ = find_peaks(spect_data['count'], height=100)  # height 값은 조정 가능

# 알려진 감마선 핵종 리스트와 에너지 값 (단위: MeV)
known_nuclides = {
    'I-131': [0.364],
    'Cs-137': [0.662],
    'Co-60': [1.173, 1.332],
    'Cs-134': [0.605, 0.796],
    'Ru-106': [0.511]
}

# 탐지된 피크와 알려진 핵종 비교
identified_nuclides = []

for peak in peaks:
    peak_energy = spect_data.loc[peak, 'energy']
    for nuclide, energies in known_nuclides.items():
        if any(abs(peak_energy - energy) < 0.05 for energy in energies):  # 허용 오차 범위는 ±0.05 MeV
            identified_nuclides.append((peak_energy, nuclide))

# 결과 출력
for energy, nuclide in identified_nuclides:
    print(f"Detected energy: {energy:.3f} MeV, Possible nuclide: {nuclide}")

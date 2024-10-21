import pymongo
import pandas as pd
from pymongo import MongoClient


client = MongoClient("mongodb://localhost:27017/")
db = client['power_plant_weather']  # 데이터베이스 이름
# 발전소별 데이터를 저장할 컬렉션
collection = db['plant_measurements']  # 발전소 데이터를 저장할 컬렉션
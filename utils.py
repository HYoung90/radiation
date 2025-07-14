from flask import Response
import csv, io
from pymongo import DESCENDING

def export_csv(collection, filename, header, fields, query=None, sort=None):
    def generate():
        buffer = io.StringIO()
        buffer.write('\ufeff')  # UTF-8 BOM
        writer = csv.writer(buffer)
        writer.writerow(header)
        yield buffer.getvalue()
        buffer.seek(0); buffer.truncate(0)

        cursor = collection.find(query or {}, {'_id':0})
        if sort:
            cursor = cursor.sort(sort)

        for doc in cursor:
            row = [ doc.get(f, '') for f in fields ]
            writer.writerow(row)
            yield buffer.getvalue()
            buffer.seek(0); buffer.truncate(0)

    return Response(
        generate(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}.csv"'}
    )

def upload_csv(collection, file_obj, field_map):
    """
    collection: MongoDB 컬렉션 객체
    file_obj: request.files['file'] 또는 io.StringIO 객체
    field_map: CSV 컬럼명 → MongoDB 필드명 매핑 dict
    """
    # 파일 객체가 Flask FileStorage인지, 아니면 StringIO/BytesIO인지 구분
    if hasattr(file_obj, 'stream'):
        raw = file_obj.stream.read()
    else:
        # StringIO 등 자체가 스트림인 경우
        file_obj.seek(0)
        raw = file_obj.read()
        # 문자열로 읽혔으면 바이트로 변환
        if isinstance(raw, str):
            raw = raw.encode('utf-8')

    # 바이너리를 텍스트로 디코딩
    text = raw.decode('utf-8')
    stream = io.StringIO(text)
    reader = csv.DictReader(stream)
    docs = []
    for row in reader:
        doc = {}
        for csv_col, mongo_field in field_map.items():
            doc[mongo_field] = row.get(csv_col)
        docs.append(doc)
    if docs:
        collection.insert_many(docs)
    return "Upload successful", 200

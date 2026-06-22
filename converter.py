"""
DWG → DXF 변환 모듈
이 PC에 설치된 ZWCAD 2018의 COM 자동화를 이용한다 (추가 설치 불필요).
AutoCAD가 없어도 ZWCAD.Application COM 으로 변환 가능함이 검증됨.
"""
import os
import time

# COM은 백그라운드 스레드에서 호출되므로 호출부에서 CoInitialize 필요
import pythoncom
import win32com.client


def convert_dwg_to_dxf(dwg_path: str, dxf_path: str, progress_cb=None) -> str:
    """
    DWG 파일을 DXF로 변환. 변환된 dxf_path 반환.
    실패 시 예외 발생.
    """
    def report(p, s):
        if progress_cb:
            progress_cb(int(p), s)

    dwg_path = os.path.abspath(dwg_path)
    dxf_path = os.path.abspath(dxf_path)
    if os.path.exists(dxf_path):
        try:
            os.remove(dxf_path)
        except Exception:
            pass

    pythoncom.CoInitialize()
    zw = None
    we_started = False
    try:
        report(8, "ZWCAD 연결 중...")
        # 이미 실행 중인 ZWCAD가 있으면 거기 붙고, 없으면 새로 띄운다
        try:
            zw = win32com.client.GetActiveObject("ZWCAD.Application")
        except Exception:
            zw = win32com.client.Dispatch("ZWCAD.Application")
            we_started = True

        try:
            zw.Visible = False
        except Exception:
            pass

        time.sleep(1.5)
        report(35, "DWG 여는 중...")
        doc = zw.Documents.Open(dwg_path)
        time.sleep(1.0)

        report(70, "DXF로 변환 중...")
        # 확장자(.dxf)로 형식 자동 판단 (검증 완료)
        doc.SaveAs(dxf_path)
        time.sleep(0.5)

        try:
            doc.Close(False)
        except Exception:
            pass

        report(95, "변환 완료")
        if not os.path.exists(dxf_path):
            raise RuntimeError("DWG 변환 결과 DXF가 생성되지 않았습니다.")
        return dxf_path
    finally:
        # 우리가 띄운 인스턴스만 종료 (사용자가 쓰던 ZWCAD는 건드리지 않음)
        if we_started and zw is not None:
            try:
                zw.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()

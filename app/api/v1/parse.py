from loguru import logger
from typing import Optional
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from app.schemas.schemas import RequestData, ResponseData
from app.core.exceptions import ParsingError, SettingsNotFoundError
from app.services.unified_parser.parser import DeliveryDocumentAssembler


router = APIRouter()


# @router.post('/parse', response_model=ResponseData)
@router.post('/parse')
async def parse_pdf_endpoint(
    data: RequestData
):
    '''На вход получает строки путей, возвращает строку'''
    logger.info('Request handler parse_pdf_endpoint')

    parser = DeliveryDocumentAssembler()

    try:
        result = parser.build_xml(upd=data.upd_file, registry=data.registry_file, protocol=data.protocol_file)

        # return ResponseData(
        #     status='Ok',
        #     content=result
        # )
        return PlainTextResponse(content=result, media_type="application/xml; charset=windows-1251")
    except Exception as e:
        return ResponseData(
            status='Error',
            content=str(e)
        )

    # parser = DeliveryDocumentAssembler()
    # result = parser.write_xml('final.xml', upd=upd_path, registry=registry_path, protocol=protocol_path)

    # save_path.write_text(result.model_dump_json(indent=2), encoding='utf-8')

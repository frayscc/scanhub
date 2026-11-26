from typing import Literal
from pydantic import BaseModel,Field

class PDFPage(BaseModel):
    id:str=Field(max_length=100)
    rotation:Literal[0,90,180,270]=0

class PDFRequest(BaseModel):
    pages:list[PDFPage]=Field(min_length=1,max_length=200)
    name:str=Field(default='合并扫描.pdf',min_length=1,max_length=180)
    a4:bool=False

import requests
from io import BytesIO
from PIL import Image
import uvicorn
import json
import numpy as np
import os
import faiss

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (Configuration,
                                  ApiClient,
                                  MessagingApi,
                                  ReplyMessageRequest,
                                  TextMessage)
from linebot.v3.webhooks import (MessageEvent,
                                 TextMessageContent,
                                 ImageMessageContent)
from linebot.v3.exceptions import InvalidSignatureError
import google.generativeai as genai
from sentence_transformers import SentenceTransformer
from typing import Dict
from contextlib import asynccontextmanager


app = FastAPI()

# ข้อมูล token และ channel secret สำหรับ LINE
ACCESS_TOKEN = os.getenv("LINE_ACCESS_TOKEN", "R/lngg68QycO4XcvPvlsbciqvWUp31SsBCWKkWOeAFJiGvc744eoRMHiT7IPkHTqT0aosHH1ChmRMQPI00l0dsirnCtoLzdIixcw8fpx+0xUxVDSfYickIWpAVVtvdvEOPpwho2/IW6PsRwkbT5HswdB04t89/1O/w1cDnyilFU=")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "aa843036e806379620e61f24153850df")

# ข้อมูล Gemini api key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyBrn9N8g0RnZrYhW-vFe3Tb2ytibKcsU3E")

# การเชื่อมต่อ และตั้งค่าข้อมูลเพื่อเรียกใช้งาน LINE Messaging API
configuration = Configuration(access_token=ACCESS_TOKEN)
handler = WebhookHandler(channel_secret=CHANNEL_SECRET)

class GeminiRAGSystem:
    def __init__(self, 
                 json_db_path: str, 
                 gemini_api_key: str, 
                 embedding_model: str = 'all-MiniLM-L6-v2'):
        # การเชื่อมต่อ และตั้งค่าข้อมูลเพื่อเรียกใช้งาน Gemini
        genai.configure(api_key=gemini_api_key)
        
        # ประกาศโมเดลที่ใช้งาน
        self.generation_model = genai.GenerativeModel('gemini-1.5-flash')
        
        # ข้อมูล JSON ที่ใช้เก็บข้อมูล
        self.json_db_path = json_db_path
        
        # โมเดลที่ใช้ในการสร้างเวกเตอร์ของข้อความ
        self.embedding_model = SentenceTransformer(embedding_model)
        
        # Load ฐานข้อมูลจากไฟล์ JSON
        self.load_database()
        
        # สร้าง FAISS index
        self.create_faiss_index()
    
    def load_database(self):
        """Load existing database or create new"""
        try:
            with open(self.json_db_path, 'r', encoding='utf-8') as f:
                self.database = json.load(f)
        except FileNotFoundError:
            self.database = {
                'documents': [],
                'embeddings': [],
                'metadata': []
            }
    
    def save_database(self):
        """Save database to JSON file"""
        with open(self.json_db_path, 'w', encoding='utf-8') as f:
            json.dump(self.database, f, indent=2, ensure_ascii=False)
     
    def add_document(self, text: str, metadata: dict = None):
        """Add document to database with embedding"""
        # ประมวลผลข้อความเพื่อหาเวกเตอร์ของข้อความ
        embedding = self.embedding_model.encode([text])[0]
        
        # เพิ่มข้อมูลลงในฐานข้อมูล
        self.database['documents'].append(text)
        self.database['embeddings'].append(embedding.tolist())
        self.database['metadata'].append(metadata or {})
        
        # Save ฐานข้อมูลลงในไฟล์ JSON
        self.save_database()
        self.create_faiss_index()
    
    def create_faiss_index(self):
        """Create FAISS index for similarity search"""
        if not self.database['embeddings']:
            return
        
        # แปลงข้อมูลเป็น numpy array
        embeddings = np.array(self.database['embeddings'], dtype='float32')
        dimension = embeddings.shape[1]

        # สร้าง FAISS index
        self.index = faiss.IndexFlatL2(dimension)
        
        # เพิ่มข้อมูลลงใน FAISS index
        self.index.add(embeddings)
    
    def retrieve_documents(self, query: str, top_k: int = 3):
        """Retrieve most relevant documents"""
        if not self.database['embeddings']:
            return []
        
        # แปลงข้อความเป็นเวกเตอร์
        query_embedding = self.embedding_model.encode([query])
        
        # ค้นหาเอกสารที่เกี่ยวข้องด้วย similarity search
        D, I = self.index.search(query_embedding, top_k)
        
        return [self.database['documents'][i] for i in I[0]]
    
    def generate_response(self, query: str):
        """Generate response using Gemini and retrieved documents"""
        # Retrieve ข้ิอมูลจากฐานข้อมูล
        retrieved_docs = self.retrieve_documents(query)
        
        # เตรียมข้อมูลเพื่อใช้ในการสร้างคำถาม
        context = "\n\n".join(retrieved_docs)
        
        # สร้าง Prompt เพื่อใช้ในการสร้างคำตอบ
        full_prompt = f"""You are an AI assistant. 
        Use the following context to answer the question precisely:

        Context:
        {context}

        Question: {query}
        
        Provide a detailed and informative response based on the context in Thai 
        but if the response is not about the context just ignore and answering in way nat."""
        
        # คำตอบจาก Gemini
        try:
            response = self.generation_model.generate_content(full_prompt)
            return response.text, full_prompt
        except Exception as e:
            return f"Error generating response: {str(e)}", str(e)
    
    def process_image_query(self, 
                            image_content: bytes, 
                            query: str,
                            use_rag: bool = True,
                            top_k_docs: int = 3) -> Dict:
        """
        Process image-based query with optional RAG enhancement
        
        Args:
            image_content (bytes): Content of the image
            query (str): Query about the image
            use_rag (bool): Whether to use RAG for context
            top_k_docs (int): Number of documents to retrieve
        
        Returns:
            Generated response about the image
        """
        # เปิดภาพจากข้อมูลที่ส่งมา
        image = Image.open(BytesIO(image_content))

        # สร้างคำอธิบายของภาพ
        initial_description = self.generation_model.generate_content(
            ["Provide a detailed, objective description of this image", image],
            generation_config={
                "max_output_tokens": 256,
                "temperature": 0.4,
                "top_p": 0.9,
                "top_k": 8
            }
        ).text
        
        # สำหรับการใช้งาน RAG 
        context = ""
        if use_rag:
            # นำคำอธิบายภาพไปใช้ในการค้นหาเอกสารที่เกี่ยวข้องใน JSON
            retrieved_docs = self.retrieve_documents(initial_description, top_k_docs)
            
            # นำข้อมูลที่ได้จากการค้นหามาใช้ในการสร้างบริบท
            context = "\n\n".join(retrieved_docs)
        
        # สร้าง Prompt สำหรับการสร้างคำตอบ
        enhanced_prompt = f"""Image Description:
        {initial_description}

        Context from Knowledge Base:
        {context}

        User Query: {query}

        Based on the image description and the contextual information from our knowledge base, 
        provide a comprehensive and insightful response to the query. 
        If the context does not directly relate to the image, focus on the image description 
        and your visual analysis in Thai."""
        
        # สร้างคำตอบจาก Gemini
        try:
            response = self.generation_model.generate_content(
                [enhanced_prompt, image],
                generation_config={
                    "max_output_tokens": 256,
                    "temperature": 0.4,
                    "top_p": 0.9,
                    "top_k": 8
                }
            )
            
            return {
                "final_response": response.text,
            }
        except Exception as e:
            return {
                "error": f"Error generating response: {str(e)}",
                "image_description": initial_description
            }
    
    def clear_database(self):
        """Clear database and save to JSON file"""
        self.database = {
            'documents': [],
            'embeddings': [],
            'metadata': []
        }
        self.save_database()

# สร้าง Object สำหรับใช้งาน Gemini
gemini = GeminiRAGSystem(
    json_db_path="gemini_rag_database.json", 
    gemini_api_key=GEMINI_API_KEY
)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ข้อมูลตัวอย่างที่ใช้สำหรับ Gemini
    sample_documents = [
    # คำถามและคำตอบเกี่ยวกับข้อมูลส่วนตัว
        {"question": "คุณชื่ออะไรครับ?", 
         "answer": "ชื่อของผมคือเสกสันต์ สุขเกษม ซึ่งชื่อของผมมีความหมายที่สำคัญต่อชีวิตที่ต้องการจะทำให้ทุกคนรู้สึกสบายใจและมีความสุข"},
        {"question": "คุณเกิดเมื่อไหร่ครับ?", 
         "answer": "ผมเกิดวันที่ 19 มิถุนายน พ.ศ. 2546 (อายุ 21 ปี) และช่วงเวลานั้นทำให้ผมมีโอกาสได้เห็นการเปลี่ยนแปลงหลายๆ อย่างในโลก"},
        {"question": "คุณเรียนที่ไหนครับ?", 
         "answer": "ผมเรียนที่มหาวิทยาลัยบูรพา สาขาวิชา Applied Artificial Intelligence และ Intelligent Technology ซึ่งผมเห็นว่า AI มีบทบาทสำคัญในการพัฒนาโลกในอนาคต"},
        {"question": "คุณอายุเท่าไหร่ครับ?", 
         "answer": "ผมอายุ 21 ปี ซึ่งในช่วงวัยนี้ ผมกำลังพยายามค้นหาตัวเองและพัฒนาในสิ่งที่รัก"},
        
        # คำถามเกี่ยวกับการศึกษา
        {"question": "ตอนนี้คุณกำลังเรียนอะไรครับ?", 
         "answer": "ตอนนี้ผมกำลังเรียนที่มหาวิทยาลัยบูรพา สาขาวิชา Applied AI และเทคโนโลยีอัจฉริยะ ซึ่งเป็นสาขาที่ผสมผสานวิทยาศาสตร์คอมพิวเตอร์และเทคโนโลยี AI ที่สามารถนำไปประยุกต์ใช้ในชีวิตจริง"},
        {"question": "คุณเรียนในสาขาวิชาอะไรครับ?", 
         "answer": "ผมเรียนในสาขา Applied Artificial Intelligence และ Intelligent Technology ซึ่งเป็นสาขาที่สามารถพัฒนาและสร้างนวัตกรรมใหม่ๆ ให้กับสังคมได้"},
        {"question": "วิชาไหนที่คุณชอบมากที่สุด?", 
         "answer": "ผมชอบวิชาคณิตศาสตร์และฟิสิกส์ เพราะมันช่วยให้ผมเห็นภาพที่ชัดเจนของจักรวาล และเข้าใจหลักการทำงานของโลกที่เราอาศัยอยู่"},
        {"question": "คุณเรียนในคณะอะไรครับ?", 
         "answer": "ผมเรียนในคณะวิทยาศาสตร์และเทคโนโลยี (Faculty of Science and Technology) ของมหาวิทยาลัยบูรพา ซึ่งทำให้ผมได้สัมผัสกับความก้าวหน้าทางเทคโนโลยีอย่างต่อเนื่อง"},
        
        # คำถามเกี่ยวกับประสบการณ์การทำงาน
        {"question": "คุณทำงานอะไรครับ?", 
         "answer": "ผมทำงานเป็นนักพัฒนาโปรแกรมที่บริษัท ClickNext ซึ่งเป็นบริษัทที่พัฒนาซอฟต์แวร์และเทคโนโลยีในการบริการลูกค้าผ่านช่องทางต่างๆ"},
        {"question": "คุณทำงานที่บริษัทไหนครับ?", 
         "answer": "ผมทำงานที่บริษัท ClickNext ซึ่งบริษัทนี้มีความมุ่งมั่นในการพัฒนาซอฟต์แวร์เพื่อช่วยให้องค์กรสามารถดำเนินงานได้อย่างมีประสิทธิภาพมากขึ้น"},
        {"question": "บริษัท ClickNext ทำอะไรครับ?", 
         "answer": "บริษัท ClickNext พัฒนาซอฟต์แวร์ที่ช่วยในการจัดการข้อมูล, ระบบการบริการลูกค้า, และเครื่องมือทางการตลาดออนไลน์ เพื่อให้ลูกค้าได้รับประสบการณ์ที่ดีขึ้น"},
        {"question": "คุณมีตำแหน่งอะไรในบริษัทครับ?", 
         "answer": "ผมทำงานในตำแหน่งนักพัฒนาโปรแกรม (Software Developer) โดยเน้นการพัฒนาระบบ AI และการประมวลผลข้อมูลในองค์กรต่างๆ"},
        
        # คำถามเกี่ยวกับชีวิตส่วนตัว
        {"question": "คุณมีความฝันอะไรครับ?", 
         "answer": "ความฝันของผมคือการมีบ้านใหญ่ที่สามารถทำให้คนรอบข้างรู้สึกสบายและมีความสุข ซึ่งผมเชื่อว่าเมื่อเราให้ความสุขกับคนอื่น เราก็จะได้รับความสุขกลับมาด้วย"},
        {"question": "อะไรทำให้คุณรู้สึกมีความสุข?", 
         "answer": "การได้ทำในสิ่งที่รักและได้ดูแลคนรอบข้างให้มีความสุขคือสิ่งที่ทำให้ผมรู้สึกเต็มเปี่ยมไปด้วยความสุขจริงๆ"},
        {"question": "คุณคิดว่าความสุขมาจากอะไร?", 
         "answer": "ความสุขมาจากการใช้ชีวิตอย่างมีความหมายและไม่มองข้ามคนรอบข้าง เราจะรู้สึกว่ามันคุ้มค่ามากขึ้นเมื่อเราให้ความสำคัญกับคนอื่นด้วยใจจริง"},
        {"question": "คุณมีความชอบอะไรบ้าง?", 
         "answer": "ผมชอบฟังเพลงและร้องเพลงเก่าๆ เพราะมันทำให้ผมรู้สึกเชื่อมโยงกับความทรงจำและช่วยผ่อนคลายความเครียดในชีวิตได้มาก"},
        
        # คำถามเกี่ยวกับความรัก
        {"question": "คุณมีความเชื่อในความรักไหมครับ?", 
         "answer": "ผมเชื่อว่าความรักเป็นสิ่งที่สำคัญในชีวิต เพราะมันช่วยสร้างความสัมพันธ์ที่ดีและเป็นกำลังใจให้กันและกันในการเผชิญกับอุปสรรค"},
        {"question": "ความรักหมายถึงอะไรสำหรับคุณ?", 
         "answer": "สำหรับผม ความรักคือการให้ความสำคัญกับคนอื่นอย่างแท้จริง ไม่ว่าจะเป็นการสนับสนุนในยามยาก การรับฟัง หรือการให้เวลาแก่คนที่เรารัก"},
        {"question": "คุณคิดว่าอะไรคือปัจจัยสำคัญในการสร้างความรักที่ยั่งยืน?", 
         "answer": "ผมคิดว่าความซื่อสัตย์, การสื่อสารที่ดี, และการเคารพซึ่งกันและกันคือปัจจัยสำคัญในการสร้างความรักที่ยั่งยืน เพราะมันทำให้เราสามารถผ่านปัญหาหรือความท้าทายไปได้"},
        {"question": "ความรักทำให้ชีวิตของคุณเปลี่ยนแปลงอย่างไร?", 
         "answer": "ความรักทำให้ผมมีมุมมองใหม่ๆ ในการใช้ชีวิต มันสอนให้ผมรู้จักการให้และการเสียสละ และทำให้ผมเข้าใจความสำคัญของการอยู่ร่วมกับผู้อื่นอย่างมีความสุข"},
        {"question": "คุณคิดอย่างไรกับความรักที่ไม่สมหวัง?", 
         "answer": "ผมเชื่อว่าความรักที่ไม่สมหวังเป็นส่วนหนึ่งของชีวิต มันช่วยให้เราเรียนรู้จากประสบการณ์และเติบโตขึ้น บางครั้งการไม่สมหวังทำให้เรามีโอกาสค้นพบสิ่งใหม่ๆ และทำให้เราเข้าใจตัวเองมากขึ้น"},
        
        # คำถามเกี่ยวกับดาราศาสตร์
        {"question": "ทฤษฎีบิ๊กแบงคืออะไร?", 
         "answer": "ทฤษฎีบิ๊กแบงคือการอธิบายการกำเนิดของจักรวาลจากการระเบิดครั้งใหญ่เมื่อประมาณ 13.8 พันล้านปีก่อน โดยจักรวาลเริ่มขยายตัวจากจุดเริ่มต้นที่หนาแน่นและร้อนมาก"},
        {"question": "หลุมดำคืออะไร?", 
         "answer": "หลุมดำเป็นวัตถุในจักรวาลที่มีมวลมหาศาลและความโน้มถ่วงสูงมากจนไม่สามารถหลบหนีได้แม้แต่แสง ซึ่งทำให้มันเป็นปรากฏการณ์ที่น่าสนใจและยังคงเป็นปริศนาในดาราศาสตร์"},
        {"question": "กาแล็กซีคืออะไร?", 
         "answer": "กาแล็กซีคือระบบของดาวเคราะห์และวัตถุอื่นๆ ที่รวมกันด้วยแรงโน้มถ่วง ซึ่งมีการหมุนวนรอบแกนกลางและเป็นหนึ่งในองค์ประกอบสำคัญของจักรวาล"},
        {"question": "ดาวฤกษ์คืออะไร?", 
         "answer": "ดาวฤกษ์เป็นวัตถุที่ปล่อยแสงและพลังงานจากกระบวนการฟิวชันในแกนกลาง เช่น ดวงอาทิตย์ที่เป็นแหล่งพลังงานหลักในระบบสุริยะของเรา"},
        {"question": "ดาวเคราะห์นอกระบบ (Exoplanet) คืออะไร?", 
         "answer": "ดาวเคราะห์นอกระบบคือดาวเคราะห์ที่โคจรรอบดาวฤกษ์อื่นๆ ที่ไม่ใช่ดวงอาทิตย์ของเรา ซึ่งเป็นหนึ่งในจุดที่นักดาราศาสตร์ให้ความสนใจในการศึกษาเกี่ยวกับชีวิตที่อาจจะเกิดขึ้นได้"},
        
        # คำถามเกี่ยวกับการศึกษาวัตถุในจักรวาล
        {"question": "การวัดระยะทางในดาราศาสตร์ใช้หน่วยอะไร?", 
         "answer": "การวัดระยะทางในดาราศาสตร์มักใช้หน่วยปีแสง ซึ่งเป็นระยะทางที่แสงเดินทางในเวลา 1 ปี"},
        {"question": "กล้องโทรทรรศน์มีบทบาทอย่างไรในดาราศาสตร์?", 
         "answer": "กล้องโทรทรรศน์เป็นเครื่องมือสำคัญในการสังเกตการณ์วัตถุในจักรวาล ช่วยให้เราเห็นดาวและกาแล็กซีที่อยู่ไกลออกไป และเปิดโอกาสในการศึกษาข้อมูลที่เราไม่สามารถเห็นด้วยตาเปล่า"}
    ]

        
    # เพิ่มข้อมูลตัวอย่างลงใน Gemini
    for doc in sample_documents:
        gemini.add_document(doc)
        
    yield

    # ลบข้อมูลที่ใช้ในการทดสอบออกจาก Gemini
    gemini.clear_database()

app = FastAPI(lifespan=lifespan)

# Endpoint สำหรับการสร้าง Webhook
@app.post('/message')
async def message(request: Request):
    # การตรวจสอบ headers จากการขอเรียกใช้บริการว่ามาจากทาง LINE Platform จริง
    signature = request.headers.get('X-Line-Signature')
    if not signature:
        raise HTTPException(
            status_code=400, detail="X-Line-Signature header is missing")

    # ข้อมูลที่ส่งมาจาก LINE Platform
    body = await request.body()

    try:
        # เรียกใช้งาน Handler เพื่อจัดข้อความจาก LINE Platform
        handler.handle(body.decode("UTF-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")


# Function สำหรับจัดการข้อมูลที่ส่งมากจาก LINE Platform
@handler.add(MessageEvent, message=(TextMessageContent, ImageMessageContent))
def handle_message(event: MessageEvent):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # ตรวจสอบ Message ว่าเป็นประเภทข้อความ Text
        if isinstance(event.message, TextMessageContent):
            # นำข้อมูลส่งไปยัง Gemini เพื่อทำการประมวลผล และสร้างคำตอบ และส่งตอบกลับมา
            gemini_response, prompt = gemini.generate_response(event.message.text)

            # Reply ข้อมูลกลับไปยัง LINE
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    replyToken=event.reply_token,
                    messages=[TextMessage(text=gemini_response)]
                )
            )

        # ตรวจสอบ Message ว่าเป็นประเภทข้อความ Image
        if isinstance(event.message, ImageMessageContent):
            # การขอข้อมูลภาพจาก LINE Service
            headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
            url = f"https://api-data.line.me/v2/bot/message/{event.message.id}/content"
            try:
                response = requests.get(url, headers=headers, stream=True)
                response.raise_for_status()
                image_data = BytesIO(response.content)
                image = Image.open(image_data)
            except Exception as e:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        replyToken=event.reply_token,
                        messages=[TextMessage(text="เกิดข้อผิดพลาด, กรุณาลองใหม่อีกครั้ง🙏🏻")]
                    )
                )
                return
            
            if image.size[0] * image.size[1] > 1024 * 1024:
                line_bot_api.reply_message_with_http_info(
                    ReplyMessageRequest(
                        replyToken=event.reply_token,
                        messages=[TextMessage(text="ขอโทษครับ ภาพมีขนาดใหญ่เกินไป กรุณาลดขนาดภาพและลองใหม่อีกครั้ง")]
                    )
                )

            try:
                # ส่งข้อมูลภาพไปยัง Gemini เพื่อทำการประมวลผล
                gemini_response = gemini.process_image_query(response.content,
                                                             query="อธิบายภาพนี้ให้ละเอียด", 
                                                             use_rag=True)
                # นำข้อมูลที่ได้จาก Gemini มาใช้งาน
                response_text = gemini_response['final_response']
            except Exception as e:
                response_text = f"เกิดข้อผิดพลาด, ไม่สามารถประมวลผลรูปภาพได้"

            # Reply ข้อมูลกลับไปยัง LINE
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    replyToken=event.reply_token, messages=[TextMessage(text=response_text)]
                )
            )

# Endpoint สำหรับทดสอบ Gemini ด้วยข้อความ
@app.get('/test-message')
async def test_message_gemini(text: str):
    """
    Debug message from Gemini
    """
    response, prompt = gemini.generate_response(text)

    return {
        "gemini_answer": response,
        "full_prompt": prompt
    }

# Endpoint สำหรับทดสอบ Gemini ด้วยรูปภาพ
@app.post('/image-query')
async def image_query(
    file: UploadFile = File(...), 
    query: str = Form("อธิบายภาพนี้ให้ละเอียด"),
    use_rag: bool = Form(True)
):
    if file.size > 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image size too large")

    # ิ อ่านข้อมูลภาพจากไฟล์ที่ส่งมา
    contents = await file.read()

    # ส่งข้อมูลภาพไปยัง Gemini เพื่อทำการประมวลผล
    image_response = gemini.process_image_query(
        image_content=contents,
        query=query,
        use_rag=use_rag
    )
    
    return image_response

if __name__ == "__main__":
    uvicorn.run("main:app",
                port=8000,
                host="0.0.0.0")

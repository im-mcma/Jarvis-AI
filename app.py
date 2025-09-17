import os
import json
import uuid
import asyncio
import logging
from datetime import datetime, timezone # Use timezone-aware datetimes
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from motor.motor_asyncio import AsyncIOMotorClient
import httpx
from dotenv import load_dotenv

# --- Load environment variables ---
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# WARNING: This is a placeholder for a single user.
# In a production app, implement a proper authentication system
# to assign a unique user ID to each user.
USER_ID = "main_user" # Consider making this dynamic based on auth later

# --- Logging setup ---
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- FastAPI app setup ---
app = FastAPI(title="Jarvis AI Chat - MongoDB Streaming")
templates = Jinja2Templates(directory="templates")

# --- MongoDB Setup ---
client: Optional[AsyncIOMotorClient] = None
db = None
USERS_COLLECTION = None

@app.on_event("startup")
async def startup_db_client():
    global client, db, USERS_COLLECTION
    logger.info("Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client["chat_ai_db"]
    USERS_COLLECTION = db["users"]
    logger.info("MongoDB connection established.")

@app.on_event("shutdown")
async def shutdown_db_client():
    if client:
        logger.info("Closing MongoDB connection...")
        client.close()
        logger.info("MongoDB client closed.")

# --- Database Functions ---
async def save_message(user_id: str, conversation_id: str, message: Dict[str, Any]):
    """Saves a single message to a conversation."""
    await USERS_COLLECTION.update_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"$push": {"conversations.$.messages": message}},
        upsert=True
    )
    logger.info(f"Message saved: User={user_id}, Conv={conversation_id}, Role={message.get('role')}")

async def create_new_conversation(user_id: str, conversation_id: str, initial_title: str = "مکالمه جدید...") -> Dict[str, Any]:
    """Creates a new conversation document for a user."""
    new_conv_doc = {
        "_id": conversation_id,
        "title": initial_title,
        "created_at": datetime.now(timezone.utc),
        "messages": []
    }
    await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$push": {"conversations": new_conv_doc}},
        upsert=True # If user doesn't exist, create user and add convo
    )
    logger.info(f"New conversation created: User={user_id}, Conv={conversation_id}, Title={initial_title}")
    return new_conv_doc

async def get_messages(user_id: str, conversation_id: str) -> List[Dict[str, Any]]:
    """Retrieves all messages for a specific conversation."""
    user_doc = await USERS_COLLECTION.find_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"conversations.$": 1}
    )
    if user_doc and "conversations" in user_doc and user_doc["conversations"]:
        messages = user_doc["conversations"][0].get("messages", [])
        logger.debug(f"Retrieved {len(messages)} messages for User={user_id}, Conv={conversation_id}")
        return messages
    logger.warning(f"No messages found for User={user_id}, Conv={conversation_id}")
    return []

async def get_conversations(user_id: str) -> List[Dict[str, Any]]:
    """Retrieves a list of all conversations for a user."""
    user_doc = await USERS_COLLECTION.find_one({"_id": user_id}, {"conversations._id": 1, "conversations.title": 1, "conversations.created_at": 1})
    if not user_doc or "conversations" not in user_doc:
        logger.info(f"No conversations found for user {user_id}")
        return []
    # Sort conversations by creation date, newest first
    conversations = sorted(user_doc["conversations"], key=lambda c: c.get("created_at", datetime.min), reverse=True)
    result = [{"id": conv["_id"], "title": conv.get("title", "مکالمه جدید..."), "created_at": conv.get("created_at").isoformat()} for conv in conversations]
    logger.debug(f"Retrieved {len(result)} conversations for user {user_id}")
    return result

async def delete_conversation(user_id: str, conversation_id: str):
    """Deletes a specific conversation."""
    result = await USERS_COLLECTION.update_one(
        {"_id": user_id},
        {"$pull": {"conversations": {"_id": conversation_id}}}
    )
    if result.modified_count == 0:
        logger.warning(f"Conversation not found or not deleted: User={user_id}, Conv={conversation_id}")
    else:
        logger.info(f"Conversation deleted: User={user_id}, Conv={conversation_id}")

async def update_conversation_title(user_id: str, conversation_id: str, new_title: str):
    """Updates the title of a specific conversation."""
    result = await USERS_COLLECTION.update_one(
        {"_id": user_id, "conversations._id": conversation_id},
        {"$set": {"conversations.$.title": new_title}}
    )
    if result.modified_count == 0:
        logger.warning(f"Could not update title for: User={user_id}, Conv={conversation_id}. Conversation not found.")
    else:
        logger.info(f"Conversation title updated: User={user_id}, Conv={conversation_id}, New Title='{new_title}'")


# --- Gemini API Utils ---
async def _call_gemini_api(payload: Dict[str, Any], model_name: str, stream: bool = False, timeout_s: int = 120):
    if not GEMINI_API_KEY:
        raise ValueError("Gemini API key is not configured.")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:{'streamGenerateContent' if stream else 'generateContent'}?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}

    timeout = httpx.Timeout(timeout_s, connect=10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status() # Raise an exception for HTTP errors (4xx or 5xx)
            return response
        except httpx.ConnectError as e:
            logger.error(f"Network error connecting to Gemini API: {e}")
            raise ConnectionError(f"Could not connect to Gemini API: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error from Gemini API: {e.response.status_code} - {e.response.text}")
            raise RuntimeError(f"Gemini API returned an error: {e.response.text}") from e
        except Exception as e:
            logger.error(f"An unexpected error occurred during API call: {e}")
            raise


async def gemini_chat_stream(messages: List[Dict[str, Any]], model: str, stop_event: asyncio.Event):
    """
    Streams the response from the Gemini API, handling JSON chunking.
    """
    logger.debug(f"Starting Gemini stream with model: {model}, message count: {len(messages)}")
    payload = {"contents": messages}
    decoder = json.JSONDecoder()
    
    # Use internal _call_gemini_api for consistency
    try:
        response = await _call_gemini_api(payload, model, stream=True)
    except Exception as e:
        yield {"type": "error", "content": str(e)}
        return

    buffer = ""
    async for chunk in response.aiter_bytes():
        if stop_event.is_set():
            logger.info("Stream stopped by client request.")
            break
        
        buffer += chunk.decode("utf-8", errors="ignore")
        
        # Process as many JSON objects as possible from the buffer
        while buffer:
            try:
                # Find the end of the first complete JSON object
                # Some API responses might be newline-delimited JSON
                if '\n' in buffer:
                    line, rest = buffer.split('\n', 1)
                    parsed_data, idx = decoder.raw_decode(line.strip())
                    # Ensure parsing from original line to get correct idx if needed, but direct `line` is often a full obj
                    buffer = rest.strip()
                else: # Try to decode directly if no newline, assume it might be a single object chunk
                    parsed_data, idx = decoder.raw_decode(buffer.strip())
                    buffer = "" # Clear buffer if successfully parsed whole
                
                # Extract text parts
                text_parts = [
                    part.get("text", "")
                    for candidate in parsed_data.get("candidates", [])
                    for part in candidate.get("content", {}).get("parts", [])
                    if part.get("text")
                ]
                
                if text_parts:
                    yield {"type": "chunk", "content": "".join(text_parts)}
                    logger.debug(f"Streamed chunk: {''.join(text_parts)[:50]}...") # Log first 50 chars
                
                # Check for finish reasons or safety ratings (metadata often sent in JSON chunks too)
                if parsed_data.get("promptFeedback") and parsed_data["promptFeedback"].get("blockReason"):
                    yield {"type": "error", "content": f"پاسخ به دلیل: {parsed_data['promptFeedback']['blockReason']} مسدود شد."}
                    stop_event.set() # Indicate a hard stop due to safety
                    break

                for candidate in parsed_data.get("candidates", []):
                    finish_reason = candidate.get("finishReason")
                    safety_ratings = candidate.get("safetyRatings")
                    if finish_reason and finish_reason != "STOP":
                        yield {"type": "warning", "content": f"مدل به دلیل {finish_reason} متوقف شد."}
                    if safety_ratings:
                        blocked_categories = [r["category"] for r in safety_ratings if r["probability"] in ["HIGH", "VERY_HIGH"]]
                        if blocked_categories:
                            yield {"type": "warning", "content": f"مسائل ایمنی در محتوا تشخیص داده شد: {', '.join(blocked_categories)}"}
                            
            except json.JSONDecodeError as e:
                # Not a complete JSON object yet, or invalid JSON, break and wait for more data
                logger.debug(f"JSONDecodeError: {e}, Buffer length: {len(buffer)}")
                break
            except Exception as e:
                logger.error(f"Error processing JSON chunk from Gemini: {e}")
                yield {"type": "error", "content": f"خطا در پردازش پاسخ مدل: {str(e)}"}
                stop_event.set()
                break

    if buffer: # Log any remaining buffer that couldn't be parsed
        logger.warning(f"Unprocessed buffer remaining after stream end: {buffer}")


async def _generate_conversation_title(user_message: str, model_response_preview: str):
    """Generates a conversation title using a separate Gemini call."""
    logger.info(f"Attempting to generate title for: '{user_message[:50]}'")
    try:
        # Keep title generation light and separate from chat history for now
        # You can add more complex context if needed.
        title_generation_prompt = [
            {"role": "user", "parts": [
                {"text": f"برای این مکالمه یک عنوان کوتاه و مناسب به فارسی پیشنهاد بده (حداکثر 10 کلمه). فقط عنوان را برگردان، بدون متن اضافی:\n\nپیام کاربر: \"{user_message}\"\nپاسخ مدل (شروع): \"{model_response_preview}\""}
            ]}
        ]
        
        # Using a more performant model for just title if possible
        response = await _call_gemini_api(title_generation_prompt, "gemini-1.0-pro", stream=False, timeout_s=30)
        json_response = response.json()
        
        generated_title_parts = [
            part.get("text", "")
            for candidate in json_response.get("candidates", [])
            for part in candidate.get("content", {}).get("parts", [])
            if part.get("text")
        ]
        
        title = "".join(generated_title_parts).strip()
        # Clean up any potential markdown or quotes from the title
        title = title.replace('"', '').replace("'", '').replace('**', '').replace('\n', ' ')
        title = ' '.join(title.split()).strip() # Remove extra spaces
        title = (title[:70] + '...') if len(title) > 70 else title # Truncate if too long

        if title and len(title) > 3: # Basic validation for a meaningful title
            logger.info(f"Generated title: '{title}'")
            return title
    except Exception as e:
        logger.error(f"Failed to generate conversation title: {e}")
    return "مکالمه نامشخص"

# --- FastAPI Routes ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/api/conversations")
async def api_get_conversations():
    convs = await get_conversations(USER_ID)
    return JSONResponse(content={"conversations": convs})

@app.get("/api/conversations/{conversation_id}/messages")
async def api_get_messages(conversation_id: str):
    msgs = await get_messages(USER_ID, conversation_id)
    return JSONResponse(content={"messages": msgs})

@app.delete("/api/conversations/{conversation_id}")
async def api_delete_conversation(conversation_id: str):
    await delete_conversation(USER_ID, conversation_id)
    return JSONResponse(content={"status": "deleted"})

@app.put("/api/conversations/{conversation_id}/title")
async def api_update_conversation_title(conversation_id: str, request: Request):
    try:
        data = await request.json()
        new_title = data.get("title")
        if not new_title or not isinstance(new_title, str) or not new_title.strip():
            raise HTTPException(status_code=400, detail="Title is required and must be a non-empty string.")
        
        await update_conversation_title(USER_ID, conversation_id, new_title.strip())
        return JSONResponse(content={"status": "updated", "new_title": new_title.strip()})
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format.")
    except Exception as e:
        logger.error(f"Error updating title for conv {conversation_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to update conversation title.")


@app.get("/api/models")
async def get_available_models():
    """Returns a list of available AI models and their descriptions."""
    # This can be dynamically fetched from Gemini API if needed,
    # but for simplicity, hardcoding common useful ones for a chat UI.
    return JSONResponse(content={
        "models": {
            "gemini-1.5-pro-latest": {"name": "Gemini 1.5 Pro", "description": "پیشرفته ترین مدل گوگل با ظرفیت بزرگ (پردازش 1M توکن).", "max_input_tokens": 1000000},
            "gemini-1.0-pro": {"name": "Gemini 1.0 Pro", "description": "نسخه قبلی مدل، سریع‌تر و کم‌هزینه‌تر برای کارهای کوچک.", "max_input_tokens": 30000}
        }
    })

# --- WebSocket Endpoint ---
@app.websocket("/api/ws/chat")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection accepted.")
    
    generation_task: Optional[asyncio.Task] = None
    stop_event = asyncio.Event()

    # Function to cancel a running generation task gracefully
    async def cancel_previous_task():
        nonlocal generation_task
        if generation_task and not generation_task.done():
            logger.info("Attempting to cancel previous generation task...")
            stop_event.set() # Signal the task to stop
            try:
                # Give it a moment to stop cleanly, otherwise cancel it forcefully
                await asyncio.wait_for(generation_task, timeout=5) 
            except (asyncio.CancelledError, asyncio.TimeoutError):
                logger.warning("Previous generation task did not stop gracefully; cancelled forcefully.")
            except Exception as e:
                logger.error(f"Unexpected error while cancelling previous task: {e}")
            finally:
                generation_task = None
                stop_event.clear() # Reset for next task

    async def chat_generator_task(message_text: str, conversation_id: Optional[str], model: str):
        """Task to handle the full chat generation process."""
        nonlocal conversation_id # Allow modifying the conversation_id from outside
        initial_conv_creation = False
        full_assistant_message = ""

        try:
            # 1. If it's a new conversation, create it first
            if not conversation_id:
                conversation_id = str(uuid.uuid4())
                await create_new_conversation(USER_ID, conversation_id)
                await websocket.send_json({"type": "info", "conversation_id": conversation_id})
                initial_conv_creation = True
                logger.info(f"Assigned new conversation_id: {conversation_id}")
            
            # 2. Get previous messages to maintain context
            conversation_history = await get_messages(USER_ID, conversation_id)
            
            # Prepare messages for the Gemini API (remove custom fields like 'timestamp')
            messages_for_api = [
                {"role": msg["role"], "parts": msg["parts"]}
                for msg in conversation_history
            ]
            
            user_message = {"role": "user", "parts": [{"text": message_text}]}
            messages_for_api.append(user_message)
            
            # 3. Save the new user message to the database
            user_message_with_ts = {"role": "user", "parts": [{"text": message_text}], "timestamp": datetime.now(timezone.utc)}
            await save_message(USER_ID, conversation_id, user_message_with_ts)

            # 4. Stream the response and build the full message
            async for result in gemini_chat_stream(messages_for_api, model, stop_event):
                if result["type"] == "chunk":
                    await websocket.send_text(result["content"])
                    full_assistant_message += result["content"]
                elif result["type"] == "error":
                    await websocket.send_json(result)
                    logger.error(f"Gemini streaming error: {result['content']}")
                    # If an error occurs, we still try to save the message content generated so far
                    break
                elif result["type"] == "warning":
                    await websocket.send_json(result) # Send warnings to client
                    logger.warning(f"Gemini streaming warning: {result['content']}")
            
            # 5. Save the complete assistant message (even if incomplete due to stop/error)
            if full_assistant_message:
                assistant_message = {"role": "model", "parts": [{"text": full_assistant_message}], "timestamp": datetime.now(timezone.utc)}
                await save_message(USER_ID, conversation_id, assistant_message)
                logger.info(f"Full assistant message saved for Conv={conversation_id}.")
            else:
                logger.warning(f"No content received from assistant for Conv={conversation_id}. Message not saved.")
                # Maybe save a placeholder message for empty responses, or skip.

            # 6. Auto-generate title if this was a new conversation and we got some response
            if initial_conv_creation and full_assistant_message:
                # Run title generation as a separate, non-blocking task after sending stream_end
                # This ensures the main chat response isn't delayed.
                async def generate_and_update_title_background():
                    new_title = await _generate_conversation_title(message_text, full_assistant_message[:200]) # Pass first 200 chars
                    if new_title and new_title != "مکالمه نامشخص":
                        await update_conversation_title(USER_ID, conversation_id, new_title)
                        await websocket.send_json({"type": "title_update", "conversation_id": conversation_id, "title": new_title})
                        logger.info(f"Conversation title updated and broadcast for Conv={conversation_id}: {new_title}")
                    else:
                         # Update title to a default if auto-generation failed or was too short
                        if message_text:
                            default_title = (message_text[:50] + "...") if len(message_text) > 50 else message_text
                            if default_title != "مکالمه جدید...": # Only update if not the initial default
                                await update_conversation_title(USER_ID, conversation_id, default_title)
                                await websocket.send_json({"type": "title_update", "conversation_id": conversation_id, "title": default_title})
                                logger.info(f"Fallback title updated and broadcast for Conv={conversation_id}: {default_title}")


                # Start background title generation but don't wait for it
                asyncio.create_task(generate_and_update_title_background())
            
            # 7. Signal the end of the stream
            await websocket.send_json({"type": "stream_end"})
            logger.info(f"Stream ended for Conv={conversation_id}.")
            
        except asyncio.CancelledError:
            logger.info(f"Chat generation task for Conv={conversation_id} was cancelled.")
            # Send stream_end in case it was cancelled mid-stream
            await websocket.send_json({"type": "stream_end"})
        except WebSocketDisconnect:
            logger.info(f"WebSocket disconnected during task for Conv={conversation_id}.")
            # No need to send anything to client if WS is already gone
        except Exception as e:
            logger.error(f"An unexpected error occurred in chat_generator_task for Conv={conversation_id}: {e}", exc_info=True)
            await websocket.send_json({"type": "error", "content": "خطای سرور: مشکلی در حین تولید پاسخ رخ داد."})
            await websocket.send_json({"type": "stream_end"}) # Ensure client can reset state


    try:
        while True:
            data_raw = await websocket.receive_text()
            data = json.loads(data_raw)
            logger.debug(f"Received WS message: {data.get('type')}, Conv ID: {data.get('conversation_id')}")
            
            if data.get("type") == "stop":
                logger.info("Received 'stop' signal from client.")
                await cancel_previous_task()
                await websocket.send_json({"type": "stream_end"}) # Confirm stop to client
                continue
                
            if data.get("type") == "chat":
                message_text = data["message"]["parts"][0]["text"]
                conversation_id = data.get("conversation_id")
                model_to_use = data.get("model", "gemini-1.5-pro-latest") # Default model
                
                await cancel_previous_task() # Stop any active task before starting a new one
                
                # Clear the stop event for the new task and start it
                stop_event.clear()
                generation_task = asyncio.create_task(chat_generator_task(message_text, conversation_id, model_to_use))
                logger.info(f"New generation task started for Conv={conversation_id}, Model={model_to_use}.")

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected from client.")
        await cancel_previous_task() # Cancel any active task on disconnect
    except Exception as e:
        logger.error(f"WebSocket connection error in main loop: {e}", exc_info=True)
        await websocket.send_json({"type": "error", "content": "اتصال شما به دلیل خطای سرور قطع شد."})
        await cancel_previous_task()

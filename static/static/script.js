// static/script.js

document.addEventListener('DOMContentLoaded', () => {
    // --- دریافت عناصر DOM ---
    const chatBox = document.getElementById('chat-box');
    const userInput = document.getElementById('user-input');
    const sendBtn = document.getElementById('send-btn');
    const imageUpload = document.getElementById('image-upload');
    const fileUpload = document.getElementById('file-upload');
    const filePreviewArea = document.getElementById('file-preview-area');
    const modelSelect = document.getElementById('model-select');
    const personaSelect = document.getElementById('persona-select');
    const tempSlider = document.getElementById('temperature');
    const tempValue = document.getElementById('temperature-value');
    const maxTokensSlider = document.getElementById('max-tokens');
    const maxTokensValue = document.getElementById('max-tokens-value');
    const clearChatBtn = document.getElementById('clear-chat-btn');
    const loadingIndicator = document.getElementById('loading-indicator');

    // --- وضعیت برنامه ---
    let chatHistory = [];
    let attachedImage = null;
    let attachedFile = null;

    // --- توابع کمکی ---

    // افزودن پیام به UI
    const addMessageToUI = (role, content) => {
        const messageDiv = document.createElement('div');
        messageDiv.className = `chat-message ${role}`;
        
        const avatarSrc = role === 'user' 
            ? 'https://i.ibb.co/qCYxMzn/user-avatar.png'
            : 'https://i.ibb.co/2FwL4qH/jarvis-avatar.png';

        messageDiv.innerHTML = `
            <img src="${avatarSrc}" alt="${role}" class="avatar">
            <div class="message-content"></div>
        `;
        // استفاده از innerHTML برای رندر کردن محتوای Markdown
        const messageContentDiv = messageDiv.querySelector('.message-content');
        messageContentDiv.innerHTML = marked.parse(content);
        
        chatBox.appendChild(messageDiv);
        chatBox.scrollTop = chatBox.scrollHeight;
        return messageContentDiv;
    };

    // نمایش پیش‌نمایش فایل
    const showFilePreview = (file, type) => {
        filePreviewArea.innerHTML = ''; // پاک کردن پیش‌نمایش‌های قبلی
        const previewItem = document.createElement('div');
        previewItem.className = 'file-preview-item';

        if (type === 'image') {
            const reader = new FileReader();
            reader.onload = e => {
                previewItem.innerHTML = `
                    <img src="${e.target.result}" alt="${file.name}">
                    <span>${file.name}</span>
                    <span class="remove-file" data-type="image">✖</span>
                `;
            };
            reader.readAsDataURL(file);
        } else {
            previewItem.innerHTML = `
                <span>📄 ${file.name}</span>
                <span class="remove-file" data-type="file">✖</span>
            `;
        }
        filePreviewArea.appendChild(previewItem);
    };

    // حذف فایل ضمیمه شده
    filePreviewArea.addEventListener('click', (e) => {
        if (e.target.classList.contains('remove-file')) {
            const type = e.target.dataset.type;
            if (type === 'image') {
                attachedImage = null;
                imageUpload.value = '';
            } else {
                attachedFile = null;
                fileUpload.value = '';
            }
            filePreviewArea.innerHTML = '';
        }
    });

    // مدیریت ارسال پیام
    const handleSendMessage = async () => {
        const userText = userInput.value.trim();
        if (!userText && !attachedImage && !attachedFile) return;

        // آماده‌سازی پیام کاربر برای نمایش
        let userMessageForUI = userText;
        if (attachedImage) userMessageForUI += `\n[تصویر: ${attachedImage.name}]`;
        if (attachedFile) userMessageForUI += `\n[فایل: ${attachedFile.name}]`;

        addMessageToUI('user', userMessageForUI);
        chatHistory.push({ role: 'user', parts: [{ text: userText }] });
        
        // پاک کردن ورودی‌ها
        userInput.value = '';
        filePreviewArea.innerHTML = '';

        setLoadingState(true);

        // --- ساخت FormData برای ارسال به بک‌اند ---
        const formData = new FormData();
        const requestData = {
            model_name: modelSelect.value,
            persona_name: personaSelect.value,
            temperature: parseFloat(tempSlider.value),
            max_tokens: parseInt(maxTokensSlider.value),
            chat_history: chatHistory.slice(0, -1) // ارسال تاریخچه بدون آخرین پیام کاربر
        };
        formData.append('request_data', JSON.stringify(requestData));

        if (attachedImage) formData.append('image', attachedImage);
        if (attachedFile) formData.append('file', attachedFile);

        // پاک کردن فایل‌های ضمیمه شده از state
        attachedImage = null;
        attachedFile = null;
        imageUpload.value = '';
        fileUpload.value = '';

        // --- ارسال درخواست و دریافت پاسخ استریم ---
        try {
            const response = await fetch('/chat', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                const errorData = await response.json();
                throw new Error(errorData.detail || 'خطا در ارتباط با سرور');
            }

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let modelResponse = '';
            const modelMessageContent = addMessageToUI('model', '...');
            
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                const chunk = decoder.decode(value, { stream: true });
                const lines = chunk.split('\n\n');

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const jsonData = JSON.parse(line.substring(6));
                        if (jsonData.token) {
                            modelResponse += jsonData.token;
                            modelMessageContent.innerHTML = marked.parse(modelResponse + ' ▌');
                            chatBox.scrollTop = chatBox.scrollHeight;
                        }
                    } else if (line.startsWith('event: error')) {
                        const errorLine = lines.find(l => l.startsWith('data: '));
                        if(errorLine){
                            const errorData = JSON.parse(errorLine.substring(6));
                            throw new Error(errorData.error);
                        }
                    }
                }
            }
            // پاک کردن نشانگر تایپ در انتها
            modelMessageContent.innerHTML = marked.parse(modelResponse);
            chatHistory.push({ role: 'model', parts: [{ text: modelResponse }] });

        } catch (error) {
            addMessageToUI('model', `**خطا:** ${error.message}`);
        } finally {
            setLoadingState(false);
        }
    };
    
    const setLoadingState = (isLoading) => {
        if (isLoading) {
            loadingIndicator.style.display = 'flex';
            sendBtn.disabled = true;
            userInput.disabled = true;
        } else {
            loadingIndicator.style.display = 'none';
            sendBtn.disabled = false;
            userInput.disabled = false;
            userInput.focus();
        }
    };

    // --- رویدادها ---
    sendBtn.addEventListener('click', handleSendMessage);
    userInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendMessage();
        }
    });

    imageUpload.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            attachedImage = e.target.files[0];
            showFilePreview(attachedImage, 'image');
        }
    });

    fileUpload.addEventListener('change', (e) => {
        if (e.target.files.length > 0) {
            attachedFile = e.target.files[0];
            showFilePreview(attachedFile, 'file');
        }
    });

    tempSlider.addEventListener('input', () => tempValue.textContent = tempSlider.value);
    maxTokensSlider.addEventListener('input', () => maxTokensValue.textContent = maxTokensSlider.value);

    clearChatBtn.addEventListener('click', () => {
        chatHistory = [];
        chatBox.innerHTML = '';
        addMessageToUI('model', 'سلام قربان، من Jarvis هستم. گفتگو پاک شد. چطور می‌توانم به شما کمک کنم؟');
    });
});

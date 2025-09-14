import React, { useState } from "react";
import { createRoot } from "react-dom/client";

function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");

  const sendMessage = async () => {
    const newMessages = [...messages, { role: "user", content: input }];
    setMessages(newMessages);
    setInput("");
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: newMessages }),
    });
    const data = await resp.json();
    const reply = data.candidates?.[0]?.content?.parts?.[0]?.text || "خطا";
    setMessages([...newMessages, { role: "assistant", content: reply }]);
  };

  return (
    <div className="min-h-screen flex flex-col bg-gray-900 text-gray-100">
      <header className="p-4 text-xl font-bold border-b border-gray-700">
        Jarvis-Gemini
      </header>
      <main className="flex-1 p-4 overflow-y-auto">
        {messages.map((m, i) => (
          <div key={i} className={`my-2 ${m.role === "user" ? "text-blue-400" : "text-green-400"}`}>
            <b>{m.role}:</b> {m.content}
          </div>
        ))}
      </main>
      <footer className="p-4 border-t border-gray-700 flex">
        <input
          className="flex-1 p-2 rounded bg-gray-800 text-white"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && sendMessage()}
        />
        <button
          className="ml-2 px-4 py-2 bg-blue-600 rounded"
          onClick={sendMessage}
        >
          ارسال
        </button>
      </footer>
    </div>
  );
}

const root = createRoot(document.getElementById("root"));
root.render(<App />);

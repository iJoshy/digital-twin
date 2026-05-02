'use client';

import { useState, useRef, useEffect } from 'react';
import Image from 'next/image';
import { BriefcaseBusiness, Bot, Mail, RotateCcw, Send, User } from 'lucide-react';

interface Message {
    id: string;
    role: 'user' | 'assistant';
    content: string;
    timestamp: Date;
}

const SESSION_STORAGE_KEY = 'digital-twin-session-id';

const QUICK_PROMPTS = [
    'Summarize Joshua for a recruiter',
    'What AI engineering work is Joshua best suited for?',
    'Why should we interview Joshua?',
    'Tell me about his cloud-native experience',
    'How can I contact Joshua about a role?',
];

function sanitizeAssistantContent(content: string): string {
    if (!content) return '';

    let cleaned = content.replace(/<thinking>[\s\S]*?<\/thinking>/gi, '');
    cleaned = cleaned.replace(/<analysis>[\s\S]*?<\/analysis>/gi, '');
    cleaned = cleaned.replace(/<reasoning>[\s\S]*?<\/reasoning>/gi, '');
    cleaned = cleaned.replace(/<\/?(thinking|analysis|reasoning)\b[^>]*>/gi, '');

    return cleaned.trim();
}

function formatTime(date: Date): string {
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function renderMarkdownLite(content: string) {
    const lines = sanitizeAssistantContent(content).split('\n');
    const elements: React.ReactNode[] = [];
    let listItems: string[] = [];

    const flushList = () => {
        if (!listItems.length) return;
        elements.push(
            <ul key={`list-${elements.length}`} className="my-2 list-disc space-y-1 pl-5">
                {listItems.map((item, index) => (
                    <li key={`${item}-${index}`}>{formatInlineMarkdown(item)}</li>
                ))}
            </ul>,
        );
        listItems = [];
    };

    lines.forEach((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) {
            flushList();
            return;
        }

        if (trimmed.startsWith('- ') || trimmed.startsWith('* ')) {
            listItems.push(trimmed.slice(2));
            return;
        }

        flushList();
        elements.push(
            <p key={`p-${index}`} className="my-2 first:mt-0 last:mb-0">
                {formatInlineMarkdown(trimmed)}
            </p>,
        );
    });

    flushList();
    return elements;
}

function formatInlineMarkdown(text: string) {
    const parts = text.split(/(\*\*[^*]+\*\*)/g);
    return parts.map((part, index) => {
        if (part.startsWith('**') && part.endsWith('**')) {
            return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        return <span key={index}>{part}</span>;
    });
}

export default function Twin() {
    const [messages, setMessages] = useState<Message[]>([]);
    const [input, setInput] = useState('');
    const [isLoading, setIsLoading] = useState(false);
    const [sessionId, setSessionId] = useState<string>('');
    const [hasAvatar, setHasAvatar] = useState(false);
    const messagesEndRef = useRef<HTMLDivElement>(null);
    const inputRef = useRef<HTMLInputElement>(null);

    const scrollToBottom = () => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    };

    useEffect(() => {
        const storedSessionId = window.localStorage.getItem(SESSION_STORAGE_KEY);
        if (storedSessionId) setSessionId(storedSessionId);

        fetch('/avatar.png', { method: 'HEAD' })
            .then(res => setHasAvatar(res.ok))
            .catch(() => setHasAvatar(false));
    }, []);

    useEffect(() => {
        scrollToBottom();
    }, [messages]);

    const sendMessage = async (content?: string) => {
        const messageContent = (content ?? input).trim();
        if (!messageContent || isLoading) return;

        const userMessage: Message = {
            id: crypto.randomUUID(),
            role: 'user',
            content: messageContent,
            timestamp: new Date(),
        };

        setMessages(prev => [...prev, userMessage]);
        setInput('');
        setIsLoading(true);

        try {
            const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/chat`, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    message: userMessage.content,
                    session_id: sessionId || undefined,
                }),
            });

            if (!response.ok) throw new Error('Failed to send message');

            const data = await response.json();

            if (!sessionId && data.session_id) {
                setSessionId(data.session_id);
                window.localStorage.setItem(SESSION_STORAGE_KEY, data.session_id);
            }

            const assistantMessage: Message = {
                id: crypto.randomUUID(),
                role: 'assistant',
                content: sanitizeAssistantContent(data.response),
                timestamp: new Date(),
            };

            setMessages(prev => [...prev, assistantMessage]);
        } catch (error) {
            console.error('Error:', error);
            const errorMessage: Message = {
                id: crypto.randomUUID(),
                role: 'assistant',
                content: 'Sorry, I hit a connection issue. Please try again in a moment.',
                timestamp: new Date(),
            };
            setMessages(prev => [...prev, errorMessage]);
        } finally {
            setIsLoading(false);
            setTimeout(() => {
                inputRef.current?.focus();
            }, 100);
        }
    };

    const resetConversation = () => {
        window.localStorage.removeItem(SESSION_STORAGE_KEY);
        setSessionId('');
        setMessages([]);
        setInput('');
        inputRef.current?.focus();
    };

    const handleKeyPress = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendMessage();
        }
    };

    return (
        <div className="flex h-full min-h-0 flex-col overflow-hidden rounded-lg border border-slate-200 bg-slate-50 shadow-lg">
            <div className="flex items-center justify-between gap-3 border-b border-slate-700 bg-slate-900 px-4 py-3 text-white">
                <div className="flex min-w-0 items-center gap-3">
                    {hasAvatar ? (
                        <Image
                            src="/avatar.png"
                            alt="Joshua Balogun"
                            width={44}
                            height={44}
                            className="h-11 w-11 rounded-full border border-slate-500 object-cover"
                        />
                    ) : (
                        <div className="flex h-11 w-11 items-center justify-center rounded-full bg-slate-700">
                            <Bot className="h-6 w-6" />
                        </div>
                    )}
                    <div className="min-w-0">
                        <h2 className="truncate text-lg font-semibold">Joshua Balogun</h2>
                        <p className="truncate text-sm text-slate-300">AI Engineer · AI Consultant · Digital Twin</p>
                    </div>
                </div>
                <button
                    type="button"
                    onClick={resetConversation}
                    className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-slate-600 text-slate-200 transition hover:bg-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-400"
                    aria-label="Start a new conversation"
                    title="Start a new conversation"
                >
                    <RotateCcw className="h-4 w-4" />
                </button>
            </div>

            <div className="flex-1 overflow-y-auto px-4 py-4">
                {messages.length === 0 && (
                    <div className="mx-auto flex max-w-2xl flex-col items-center text-center">
                        {hasAvatar ? (
                            <Image
                                src="/avatar.png"
                                alt="Joshua Balogun"
                                width={80}
                                height={80}
                                className="mb-4 h-20 w-20 rounded-full border border-slate-300 object-cover shadow-sm"
                            />
                        ) : (
                            <Bot className="mb-4 h-14 w-14 text-slate-500" />
                        )}
                        <h3 className="text-xl font-semibold text-slate-900">Chat with Joshua&apos;s digital twin</h3>
                        <p className="mt-2 max-w-xl text-sm leading-6 text-slate-600">
                            Ask about Joshua&apos;s AI engineering work, cloud-native experience, career background,
                            consulting fit, or how to start a serious opportunity conversation.
                        </p>

                        <div className="mt-5 grid w-full gap-2 sm:grid-cols-2">
                            {QUICK_PROMPTS.map(prompt => (
                                <button
                                    key={prompt}
                                    type="button"
                                    onClick={() => sendMessage(prompt)}
                                    disabled={isLoading}
                                    className="rounded-md border border-slate-200 bg-white px-3 py-2 text-left text-sm text-slate-700 shadow-sm transition hover:border-slate-400 hover:bg-slate-100 focus:outline-none focus:ring-2 focus:ring-slate-500 disabled:cursor-not-allowed disabled:opacity-60"
                                >
                                    {prompt}
                                </button>
                            ))}
                        </div>
                    </div>
                )}

                <div className="space-y-4">
                    {messages.map(message => (
                        <div
                            key={message.id}
                            className={`flex gap-3 ${message.role === 'user' ? 'justify-end' : 'justify-start'}`}
                        >
                            {message.role === 'assistant' && (
                                <div className="shrink-0">
                                    {hasAvatar ? (
                                        <Image
                                            src="/avatar.png"
                                            alt="Joshua Balogun"
                                            width={32}
                                            height={32}
                                            className="h-8 w-8 rounded-full border border-slate-300 object-cover"
                                        />
                                    ) : (
                                        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-800">
                                            <Bot className="h-5 w-5 text-white" />
                                        </div>
                                    )}
                                </div>
                            )}

                            <div
                                className={`max-w-[82%] rounded-lg px-3 py-2 text-sm leading-6 sm:max-w-[72%] ${
                                    message.role === 'user'
                                        ? 'bg-slate-800 text-white'
                                        : 'border border-slate-200 bg-white text-slate-800 shadow-sm'
                                }`}
                            >
                                <div className="whitespace-pre-wrap">
                                    {message.role === 'assistant'
                                        ? renderMarkdownLite(message.content)
                                        : message.content}
                                </div>
                                <p
                                    className={`mt-1 text-xs ${
                                        message.role === 'user' ? 'text-slate-300' : 'text-slate-500'
                                    }`}
                                >
                                    {formatTime(message.timestamp)}
                                </p>
                            </div>

                            {message.role === 'user' && (
                                <div className="shrink-0">
                                    <div className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-600">
                                        <User className="h-5 w-5 text-white" />
                                    </div>
                                </div>
                            )}
                        </div>
                    ))}

                    {isLoading && (
                        <div className="flex gap-3">
                            <div className="shrink-0">
                                {hasAvatar ? (
                                    <Image
                                        src="/avatar.png"
                                        alt="Joshua Balogun"
                                        width={32}
                                        height={32}
                                        className="h-8 w-8 rounded-full border border-slate-300 object-cover"
                                    />
                                ) : (
                                    <div className="flex h-8 w-8 items-center justify-center rounded-full bg-slate-800">
                                        <Bot className="h-5 w-5 text-white" />
                                    </div>
                                )}
                            </div>
                            <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600 shadow-sm">
                                Joshua&apos;s twin is thinking...
                            </div>
                        </div>
                    )}
                </div>
                <div ref={messagesEndRef} />
            </div>

            <div className="border-t border-slate-200 bg-white p-4">
                <div className="mb-3 flex flex-wrap gap-2">
                    <span className="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-600">
                        <BriefcaseBusiness className="h-3.5 w-3.5" />
                        Recruiter friendly
                    </span>
                    <span className="inline-flex items-center gap-1 rounded-md bg-slate-100 px-2 py-1 text-xs text-slate-600">
                        <Mail className="h-3.5 w-3.5" />
                        Follow-up handoff available
                    </span>
                </div>
                <div className="flex gap-2">
                    <input
                        ref={inputRef}
                        type="text"
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        onKeyDown={handleKeyPress}
                        placeholder="Ask about Joshua's experience, role fit, consulting, or availability..."
                        className="min-w-0 flex-1 rounded-md border border-slate-300 px-4 py-2 text-sm text-slate-900 focus:border-transparent focus:outline-none focus:ring-2 focus:ring-slate-700"
                        disabled={isLoading}
                        autoFocus
                    />
                    <button
                        type="button"
                        onClick={() => sendMessage()}
                        disabled={!input.trim() || isLoading}
                        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-slate-800 text-white transition hover:bg-slate-900 focus:outline-none focus:ring-2 focus:ring-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
                        aria-label="Send message"
                        title="Send message"
                    >
                        <Send className="h-5 w-5" />
                    </button>
                </div>
            </div>
        </div>
    );
}

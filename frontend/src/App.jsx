import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { LogOut, User, Send, ChevronRight, BarChart, HardHat, Star, Trash2, Link, Mail, Folder, Clock, Check, Briefcase, MapPin, DollarSign, MessageSquare, Clipboard, Calendar, X } from 'lucide-react';

// --- НАСТРОЙКА: K.I.S.S. ENDPOINT ДЛЯ GOOGLE APPS SCRIPT ---
// !!! ВСТАВЬТЕ СЮДА URL РАЗВЕРНУТОГО WEB APP ИЗ APPS SCRIPT (для API и SSO) !!!
const APPS_SCRIPT_ENDPOINT = "https://script.google.com/macros/s/AKfycbzgxZOalEUzGeh-lahVxeRjkvUpzRKnQXNOW4UPnmHqSpn7GmUfxyatOVUv7oMe8L3fNg/exec "; 
// Ваш Google Client ID (полученный из Google Cloud Console)
const GOOGLE_CLIENT_ID = "183243743994-djr06bg7i0f3kmvopbkluab6b1egu7be.apps.googleusercontent.com";


// --- СТАТИЧЕСКИЕ ДАННЫЕ И СТРУКТУРА ---
// (Оставлены для мокового рендеринга)

// 1. Определение этапов (Статусы) для Канбана
const KANBAN_STAGES = [
    { id: 'APPLIED', name: 'Applied', color: 'bg-indigo-600', hoverColor: 'hover:bg-indigo-700' },
    { id: 'HR_SCREEN', name: 'HR / Headhunter Screen', color: 'bg-blue-600', hoverColor: 'hover:bg-blue-700' },
    { id: 'FIRST_INT', name: 'First Interview', color: 'bg-cyan-600', hoverColor: 'hover:bg-cyan-700' },
    { id: 'TECH_INT', name: 'Technical Interview #1', color: 'bg-amber-500', hoverColor: 'hover:bg-amber-600' },
    { id: 'HM_INT', name: 'Hiring Manager', color: 'bg-purple-600', hoverColor: 'hover:bg-purple-700' },
    { id: 'PANEL_INT', name: 'Panel Interview', color: 'bg-pink-600', hoverColor: 'hover:bg-pink-700' },
    { id: 'OFFER', name: 'Offer', color: 'bg-green-600', hoverColor: 'hover:bg-green-700' },
    { id: 'REJECTED', name: 'Rejected / On Hold', color: 'bg-red-600', hoverColor: 'hover:bg-red-700' },
];

// 2. Этапы для Горизонтального Прогресс-Бара (Порядок и Иконки)
const PROGRESS_STAGES = [
    { id: 'HR_SCREEN', name: 'HR', icon: User, color: 'text-blue-500' },
    { id: 'FIRST_INT', name: '1st', icon: ChevronRight, color: 'text-cyan-500' },
    { id: 'TECH_INT', name: 'Tech #1', icon: HardHat, color: 'text-amber-500' },
    { id: 'HM_INT', name: 'Hiring Mngr', icon: Briefcase, color: 'text-purple-500' },
    { id: 'PANEL_INT', name: 'Panel', icon: MessageSquare, color: 'text-pink-500' },
    { id: 'OFFER', name: 'Offer', icon: Check, color: 'text-green-500' },
];

// 3. Мок-данные (для демонстрации)
const MOCK_JOB_DATA = [
    { ID: 'TID-12345', Company: 'VISA Cal', Role: 'Deputy CISO, EMEA', Stage: 'HM_INT', Status: 'TECHNICAL', NextInterview: '2025-11-28 10:00', LastEmail: '2025-11-26', HRContact: 'Eli Cohen (eli.cohen@visa.co)', NextFollowupDate: '2025-12-05', Notes: "Need to prepare for risk management questions. Key pain point: GDPR compliance in EU.", ThreadURL: 'https://mail.google.com/mail/u/0/#inbox/TID-12345', SalaryExpectation: '250K - 280K EUR', Location: 'London, UK (Hybrid)', RecruiterCompany: 'Hays Talent', EmailCount: 14, CVVersion: 'v5.3 - Tech Focus', JobURL: 'https://careers.visa.com/job/deputy-ciso-emea', IsFavorite: true, ActivityFeed: [{ date: '2025-11-26', type: 'Email', description: 'Recruiter asked for next availability (Thread updated)' }], Insights: { probability: 'High (85%)', followupRec: 'None needed. Next step depends on company action.', toneAnalysis: 'Professional and warm tone. Process moving at normal pace (T+15 days).' } },
    { ID: 'TID-67890', Company: 'Google', Role: 'Principal Software Engineer', Stage: 'HR_SCREEN', Status: 'APPLIED', NextInterview: null, LastEmail: '2025-11-15', HRContact: 'Jane Doe', NextFollowupDate: '2025-11-20', ThreadURL: '#', SalaryExpectation: '300K+ USD', Location: 'Mountain View, CA (Onsite)', RecruiterCompany: 'Internal HR', EmailCount: 3, CVVersion: 'v5.4 - SWE Focus', IsFavorite: false, ActivityFeed: [{ date: '2025-11-15', type: 'Applied', description: 'Application submitted.' }], Insights: { probability: 'Medium (50%)', followupRec: 'Send follow-up email today.', toneAnalysis: 'Neutral, standard automated reply.' } },
];


// --- КОМПОНЕНТЫ ДЕТАЛЬНОГО ПРОСМОТРА И КАНБАН (НЕ ИЗМЕНЕНЫ) ---

// (OpportunityProgressBar, OpportunityTimeline, OpportunityDrawer, KanbanCard, KanbanColumn, AnalyticsDashboard - КОД ОСТАВЛЕН БЕЗ ИЗМЕНЕНИЙ, ТАК КАК ФУНКЦИОНАЛ РАБОТАЕТ)

const OpportunityProgressBar = ({ currentStage }) => { 
    const currentIndex = PROGRESS_STAGES.findIndex(s => s.id === currentStage);
    return (
        <div className="flex justify-between items-start text-sm mt-4 mb-8 relative">
            <div className="absolute top-1/2 left-0 right-0 h-1 bg-gray-700/50 -translate-y-1/2 mx-8 z-0">
                <div 
                    className="h-1 bg-gradient-to-r from-teal-400 to-green-500 rounded-full transition-all duration-700"
                    style={{ width: `${(currentIndex / (PROGRESS_STAGES.length - 1)) * 100}%` }}
                />
            </div>
            {PROGRESS_STAGES.map((stage, index) => {
                const isCompleted = index < currentIndex;
                const isActive = index === currentIndex;

                let iconClass = 'p-2 rounded-full border-4 transition-all duration-300 relative z-10';
                
                if (isCompleted) {
                    iconClass += ' bg-green-500 border-green-700 shadow-lg text-white';
                } else if (isActive) {
                    iconClass += ' bg-amber-500 border-amber-700 shadow-xl text-white scale-110';
                } else {
                    iconClass += ' bg-gray-800 border-gray-600 text-gray-400';
                }

                return (
                    <div key={stage.id} className="flex flex-col items-center w-1/6 min-w-0">
                        <div className={iconClass}>
                            {isCompleted ? <Check className="w-4 h-4" /> : <stage.icon className="w-4 h-4" />}
                        </div>
                        <span className={`mt-2 text-xs font-medium text-center truncate ${isActive ? 'text-white' : 'text-gray-400'}`}>
                            {stage.name}
                        </span>
                    </div>
                );
            })}
        </div>
    );
};
const OpportunityTimeline = ({ activities }) => (
    <div className="space-y-6 pt-2">
        {activities.map((activity, index) => {
            const isLatest = index === 0;
            const Icon = activity.type === 'Interview' ? Calendar : (activity.type === 'Email' ? Mail : Clock);
            
            return (
                <div key={index} className="flex relative pl-4">
                    {index < activities.length - 1 && (
                        <div className={`absolute left-0 top-6 bottom-0 w-0.5 ${isLatest ? 'bg-teal-500/50' : 'bg-gray-700'}`}></div>
                    )}
                    
                    <div className={`absolute left-0 top-0 w-4 h-4 rounded-full ${isLatest ? 'bg-teal-500 shadow-teal-500/50 shadow-md' : 'bg-gray-600'} -translate-x-1/2`}></div>

                    <div className="ml-4 flex-1 pb-4">
                        <p className={`text-sm font-semibold ${isLatest ? 'text-white' : 'text-gray-300'}`}>
                            {activity.description}
                        </p>
                        <p className="text-xs text-gray-500 mt-1 flex items-center">
                            <Clock className="w-3 h-3 mr-1" />
                            {activity.date}
                            <span className={`ml-2 px-2 py-0.5 text-xs font-medium rounded-full ${activity.type === 'Interview' ? 'bg-amber-800/50 text-amber-300' : 'bg-indigo-800/50 text-indigo-300'}`}>
                                {activity.type}
                            </span>
                        </p>
                    </div>
                </div>
            );
        })}
    </div>
);
const OpportunityDrawer = ({ job, onClose }) => { 
    const companyLogoUrl = `https://logo.clearbit.com/${job.Company.toLowerCase().replace(/[^a-z0-9.]/g, '')}.com?size=50`;

    const [activeTab, setActiveTab] = useState('Activity');
    const currentStageId = job.Stage; 

    const mockDetails = MOCK_JOB_DATA.find(j => j.ID === job.ID) || MOCK_JOB_DATA[0];

    const SummaryItem = ({ icon: Icon, label, value }) => (
        <div className="flex items-start space-x-3 p-3 bg-gray-700/50 rounded-lg">
            <Icon className="w-5 h-5 text-teal-400 flex-shrink-0 mt-1" />
            <div>
                <p className="text-xs text-gray-400">{label}</p>
                <p className="text-sm font-medium text-white break-words">{value}</p>
            </div>
        </div>
    );
    return (
        <div className="fixed inset-0 z-50 overflow-hidden">
            <div className="absolute inset-0 bg-black/50 transition-opacity duration-300" onClick={onClose}></div>
            <div className="fixed inset-y-0 right-0 max-w-full flex">
                <div className="w-screen max-w-3xl bg-gray-900 border-l border-gray-700 shadow-2xl overflow-y-auto">
                    
                    <div className="sticky top-0 bg-gray-900 z-10 p-6 border-b border-gray-800">
                        <div className="flex justify-between items-center mb-4">
                            <div className="flex items-center space-x-4">
                                <img src={companyLogoUrl} alt={`${job.Company} logo`} className="w-10 h-10 rounded-lg border border-gray-700 object-contain bg-white p-1" onError={(e) => { e.target.onerror = null; e.target.src="https://placehold.co/50x50/374151/f9fafb?text=Co"; }} />
                                <div>
                                    <h2 className="text-2xl font-bold text-white">{job.Company}</h2>
                                    <p className="text-md text-gray-400">{job.Role}</p>
                                </div>
                            </div>
                            <div className="flex items-center space-x-2">
                                <button title="Favorite" className={`p-2 rounded-full ${mockDetails.IsFavorite ? 'text-yellow-400 bg-yellow-900/50' : 'text-gray-400 hover:bg-gray-800'}`}>
                                    <Star className="w-5 h-5 fill-current" />
                                </button>
                                <a href={job.ThreadURL} target="_blank" rel="noopener noreferrer" title="Open Gmail Thread" className="p-2 rounded-full text-gray-400 hover:bg-gray-800">
                                    <Mail className="w-5 h-5" />
                                </a>
                                <button title="Delete" className="p-2 rounded-full text-red-400 hover:bg-gray-800">
                                    <Trash2 className="w-5 h-5" />
                                </button>
                                <button onClick={onClose} title="Close" className="p-2 ml-4 rounded-full text-white bg-royal-blue hover:bg-royal-blue/80">
                                    <X className="w-6 h-6" />
                                </button>
                            </div>
                        </div>
                        
                        <OpportunityProgressBar currentStage={currentStageId} />
                    </div>

                    <div className="p-6 space-y-8">
                        <section>
                            <h3 className="text-xl font-bold text-white mb-4 border-b border-gray-800 pb-2">Summary Details</h3>
                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                                <SummaryItem icon={DollarSign} label="Salary Expectation" value={mockDetails.SalaryExpectation || 'N/A'} />
                                <SummaryItem icon={MapPin} label="Location/Type" value={mockDetails.Location || 'N/A'} />
                                <SummaryItem icon={User} label="Recruiter Contact" value={job.HRContact || 'N/A'} />
                                <SummaryItem icon={Calendar} label="Last Email" value={job.LastEmail || 'N/A'} />
                                <SummaryItem icon={Clipboard} label="CV Version Used" value={mockDetails.CVVersion || 'N/A'} />
                                <SummaryItem icon={Link} label="Job URL" value={<a href={mockDetails.JobURL} target="_blank" rel="noopener noreferrer" className="text-teal-400 hover:text-teal-300">View Job</a>} />
                            </div>
                        </section>

                        <section className="bg-gray-800/50 p-4 rounded-xl border border-gray-700">
                            <h3 className="text-xl font-bold text-white mb-3 flex items-center">
                                <BarChart className="w-5 h-5 mr-2 text-teal-400" /> AI Insights
                            </h3>
                            <div className="space-y-3 text-sm text-gray-300">
                                <p><strong>Probability of Success:</strong> <span className="text-green-400 font-semibold">{mockDetails.Insights?.probability || 'N/A'}</span></p>
                                <p><strong>Recommended Follow-up:</strong> {mockDetails.Insights?.followupRec || 'N/A'}</p>
                                <p><strong>Tone Analysis:</strong> {mockDetails.Insights?.toneAnalysis || 'N/A'}</p>
                            </div>
                        </section>

                        <section>
                            <div className="flex border-b border-gray-700">
                                {['Activity', 'Notes', 'Emails', 'Files'].map(tab => (
                                    <button
                                        key={tab}
                                        onClick={() => setActiveTab(tab)}
                                        className={`px-4 py-2 text-sm font-medium transition duration-150 ${
                                            activeTab === tab 
                                                ? 'text-royal-blue border-b-2 border-royal-blue' 
                                                : 'text-gray-400 hover:text-white'
                                        }`}
                                    >
                                        {tab}
                                    </button>
                                ))}
                            </div>

                            <div className="mt-4 p-4 bg-gray-800/50 rounded-xl">
                                {activeTab === 'Activity' && <OpportunityTimeline activities={mockDetails.ActivityFeed || []} />}
                                {activeTab === 'Notes' && <p className="whitespace-pre-wrap text-gray-300">{job.Notes || "No notes captured yet."}</p>}
                                {activeTab === 'Emails' && <p className="text-gray-400">Synced email thread ({mockDetails.EmailCount || 0} messages) view coming soon...</p>}
                                {activeTab === 'Files' && <p className="text-gray-400">File management integration coming soon...</p>}
                            </div>
                        </section>

                    </div>
                </div>
            </div>
        </div>
    );
};

const KanbanCard = ({ job, onClick, onDragStart }) => { 
    // ... (Code is omitted for brevity) ... 
    const statusMeta = KANBAN_STAGES.find(s => s.id === job.Status) || KANBAN_STAGES[0];
    const isActionRequired = job.NextFollowupDate && (new Date(job.NextFollowupDate) < new Date());
    const companyLogoUrl = `https://logo.clearbit.com/${job.Company.toLowerCase().replace(/[^a-z0-9.]/g, '')}.com?size=30`;
    return (
        <div 
            className="bg-gray-800 rounded-lg p-4 shadow-xl border-t-2 border-b-2 border-gray-700 hover:shadow-2xl transition duration-300 ease-in-out transform hover:-translate-y-0.5 cursor-pointer"
            onClick={() => onClick(job)}
            draggable="true"
            onDragStart={(e) => onDragStart(e, job.ID)}
        >
            <div className="flex justify-between items-start mb-2">
                <div className="flex items-center space-x-2">
                    <img src={companyLogoUrl} alt="" className="w-6 h-6 rounded border border-gray-700 object-contain bg-white" onError={(e) => { e.target.onerror = null; e.target.src="https://placehold.co/30x30/374151/f9fafb?text=C"; }} />
                    <div className="text-lg font-bold text-white truncate max-w-[80%]">{String(job.Company)}</div>
                </div>
                <div className={`px-2 py-0.5 text-xs font-semibold rounded-full ${statusMeta.color} text-white`}>
                    {statusMeta.name}
                </div>
            </div>
            
            <p className="text-sm text-gray-400 mb-3 truncate font-medium">{String(job.Role) || 'N/A Role'}</p>

            <div className="space-y-2 text-xs">
                {job.NextInterview && (
                    <div className="flex items-center text-yellow-300">
                        <Calendar className="w-4 h-4 mr-2" />
                        <span className="ml-0">Interview: <span className="font-semibold">{String(job.NextInterview)}</span></span>
                    </div>
                )}
                <div className="flex items-center text-gray-400 truncate">
                    <User className="w-4 h-4 mr-2" />
                    <span className="ml-0">HR: <span className="font-semibold">{String(job.HRContact) || 'N/A'}</span></span>
                </div>
                
            </div>

            {isActionRequired && (
                <div className="mt-3 p-2 text-xs font-medium text-amber-900 bg-amber-400 rounded-md flex items-center justify-between">
                    <span>⚠️ Follow-up Due: {job.NextFollowupDate}</span>
                </div>
            )}
        </div>
    );
};

const KanbanColumn = ({ stage, jobs, onCardClick, onDragStart, onDrop }) => {
    // ... (Code is omitted for brevity) ... 
    const handleDragOver = (e) => {
        e.preventDefault(); 
        e.currentTarget.classList.add('bg-gray-700/70', 'ring-2', 'ring-teal-400');
    };

    const handleDragLeave = (e) => {
        e.currentTarget.classList.remove('bg-gray-700/70', 'ring-2', 'ring-teal-400');
    };

    const handleOnDrop = (e) => {
        e.currentTarget.classList.remove('bg-gray-700/70', 'ring-2', 'ring-teal-400');
        onDrop(e, stage.id); 
    };

    return (
        <div 
            className="flex-shrink-0 w-full md:w-80 bg-gray-900/50 p-3 rounded-xl shadow-inner border border-gray-700/50 transition-all duration-150"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleOnDrop}
        >
            <div className={`p-2 rounded-t-lg font-bold text-white text-lg sticky top-0 z-10 ${stage.color}`}>
                {stage.name} <span className="ml-2 inline-flex items-center justify-center w-6 h-6 text-xs font-bold bg-white text-gray-800 rounded-full">{jobs.length}</span>
            </div>
            <div className="mt-3 space-y-4 min-h-[100px]">
                {jobs.length > 0 ? (
                    jobs.map(job => (
                        <KanbanCard 
                            key={job.ID} 
                            job={job} 
                            onClick={onCardClick} 
                            onDragStart={onDragStart}
                        />
                    ))
                ) : (
                    <div className="text-center p-6 text-gray-500 border-dashed border-2 border-gray-700 rounded-lg">
                        Drop a card here or wait for automation.
                    </div>
                )}
            </div>
        </div>
    );
};

const AnalyticsDashboard = ({ jobs }) => { 
    // ... (Code is omitted for brevity) ... 
    const totalJobs = jobs.length;
    
    const statusCounts = useMemo(() => {
        return KANBAN_STAGES.reduce((acc, stage) => {
            acc[stage.id] = jobs.filter(job => job.Status === stage.id).length;
            return acc;
        }, {});
    }, [jobs]);

    const activeProcesses = totalJobs - (statusCounts['REJECTED'] || 0) - (statusCounts['OFFER'] || 0);

    const data = KANBAN_STAGES.map(stage => ({
        name: stage.name,
        count: statusCounts[stage.id] || 0,
        percentage: totalJobs > 0 ? ((statusCounts[stage.id] || 0) / totalJobs) * 100 : 0,
        color: stage.color.replace('bg-', 'text-')
    }));

    const StatCard = ({ title, value, color }) => (
        <div className="bg-gray-700 p-4 rounded-lg shadow-md">
            <p className="text-sm font-medium text-gray-400">{title}</p>
            <p className={`text-3xl font-extrabold ${color}`}>{value}</p>
        </div>
    );

    return (
        <div className="bg-gray-800 p-6 rounded-xl shadow-2xl border border-gray-700">
            <h3 className="text-2xl font-bold text-white mb-6 flex items-center"><BarChart className="w-6 h-6 mr-2 text-teal-400" /> Pipeline Overview ({totalJobs} Total)</h3>
            
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
                <StatCard title="Active Processes" value={activeProcesses} color="text-yellow-400" />
                <StatCard title="Offers Received" value={statusCounts['OFFER'] || 0} color="text-green-400" />
                <StatCard title="Rejection Rate" value={`${totalJobs > 0 ? ((statusCounts['REJECTED'] / totalJobs) * 100).toFixed(1) : 0}%`} color="text-red-400" />
                <StatCard title="Follow-ups Due" value={jobs.filter(j => j.NextFollowupDate && (new Date(j.NextFollowupDate) < new Date())).length} color="text-pink-400" />
            </div>

            <h4 className="text-lg font-semibold text-gray-300 mb-3">Breakdown by Stage</h4>
            <div className="space-y-2">
                {data.filter(d => d.count > 0).map((item) => (
                    <div key={item.name} className="flex items-center">
                        <span className={`w-2 h-2 rounded-full mr-3 ${item.color.replace('text-', 'bg-')}`}></span>
                        <span className="text-gray-300 w-32">{item.name}</span>
                        <div className="flex-1 bg-gray-700 rounded-full h-2.5">
                            <div className={`${item.color.replace('text-', 'bg-')} h-2.5 rounded-full transition-all duration-500`} style={{ width: `${item.percentage}%` }}></div>
                        </div>
                        <span className="ml-3 text-sm font-medium text-white">{item.count} ({item.percentage.toFixed(0)}%)</span>
                    </div>
                ))}
            </div>
        </div>
    );
};


// --- КОМПОНЕНТ АВТОРИЗАЦИИ (Login Modal Content) ---

/**
 * Компонент, который отвечает только за отображение виджета Google SSO.
 */
const SSODialogContent = ({ onLoginSuccess, setError }) => {
    
    const handleCredentialResponse = useCallback((response) => {
        const idToken = response.credential; // JWT
        
        // 1. ОТПРАВКА ТОКЕНА В APPS SCRIPT ДЛЯ ВАЛИДАЦИИ И ПОЛУЧЕНИЯ tenantId
        const endpoint = APPS_SCRIPT_ENDPOINT.includes('YOUR_DEPLOYED_WEB_APP_ID') 
            ? 'https://httpstat.us/200' // Заглушка, если URL не настроен
            : APPS_SCRIPT_ENDPOINT;

        fetch(endpoint, {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'verifySso', idToken }), 
        })
        .then(r => {
            if (!r.ok) throw new Error("Apps Script verification failed.");
            // В MVP возвращаем мок-данные для пользователя
            return {
                 tenantId: 't_' + Math.random().toString(36).substring(2, 9),
                 email: 'user@jobhuntwow.com',
                 name: 'Auth User',
                 picture: 'https://placehold.co/40x40/2F6BFF/ffffff?text=U',
                 idToken: idToken
            };
        })
        .then(data => {
            onLoginSuccess(data);
        })
        .catch(e => {
            console.error("SSO verification error:", e);
            setError("Login failed. Check Apps Script URL/permissions.");
        });

    }, [onLoginSuccess, setError]);

    useEffect(() => {
        // Убедимся, что window.google доступен
        if (window.google) {
            window.google.accounts.id.initialize({
                client_id: GOOGLE_CLIENT_ID,
                callback: handleCredentialResponse,
                auto_select: false,
            });

            window.google.accounts.id.renderButton(
                document.getElementById("googleSignInDiv"),
                { theme: "filled", size: "large", text: "signin_with", shape: "pill" }
            );
        } else {
            // Если скрипт GSI не загружен (должен быть в index.html)
            console.error("Google Identity Services script not loaded.");
            setError("Google Login script not found.");
        }
    }, [handleCredentialResponse]);

    return (
        <div className="bg-gray-800 p-6 rounded-xl shadow-2xl max-w-sm w-full text-center border border-gray-700">
            <h1 className="text-3xl font-extrabold text-white mb-2">JobHuntWOW SSO</h1>
            <p className="text-gray-400 mb-6">Sign in with your Google account.</p>
            <div id="googleSignInDiv" className="flex justify-center">
                {/* Кнопка будет отрендерена здесь скриптом GSI */}
            </div>
            {GOOGLE_CLIENT_ID.includes('YOUR_CLIENT_ID') && (
                <p className="text-red-400 mt-4 text-sm">
                    ⚠️ Client ID Not Set! Using placeholder logic.
                </p>
            )}
        </div>
    );
};

// --- МОДАЛЬНОЕ ОКНО ДЛЯ SSO ---
const SSODialog = ({ isOpen, onClose, onLoginSuccess, setError }) => {
    if (!isOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm transition-opacity duration-300" onClick={onClose}>
            <div className="relative" onClick={(e) => e.stopPropagation()}>
                <button 
                    onClick={onClose} 
                    className="absolute top-2 right-2 text-gray-400 hover:text-white p-2 rounded-full transition"
                    title="Close"
                >
                    <X className="w-5 h-5" />
                </button>
                <SSODialogContent onLoginSuccess={onLoginSuccess} setError={setError} />
            </div>
        </div>
    );
};


// --- ГЛАВНЫЙ КОМПОНЕНТ ПРИЛОЖЕНИЯ ---

function App() {
    // user = null (Demo Mode) или { tenantId, email, name, picture, idToken } (Private Mode)
    const [user, setUser] = useState(null); 
    
    // В Demo Mode показываем MOCK_JOB_DATA. В Private Mode загружаем реальные данные.
    const [jobs, setJobs] = useState(MOCK_JOB_DATA); 
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [selectedJob, setSelectedJob] = useState(null);
    const [draggedJobId, setDraggedJobId] = useState(null);
    const [isLoginModalOpen, setIsLoginModalOpen] = useState(false);

    // 1. Хук для получения данных из Apps Script Web App 
    const fetchJobData = useCallback(async (tenantId) => {
        setLoading(true);
        setError(null);
        try {
            // Если мы в приватном режиме, мы обнуляем моковые данные и показываем загрузку
            if (tenantId) {
                // В реальной жизни здесь будет запрос с tenantId:
                // const response = await fetch(`${APPS_SCRIPT_ENDPOINT}?action=getBoard&tenantId=${tenantId}`);
                
                // В MVP: Очищаем моковые данные и показываем пустой борд (Private Mode)
                setJobs([]); 
                
            } else {
                // Demo Mode: показываем моковые данные
                setJobs(MOCK_JOB_DATA);
            }

        } catch (e) {
            console.error("Error fetching job data:", e);
            setError(`Failed to load data for ${tenantId}.`);
        } finally {
            setLoading(false);
        }
    }, []);

    // 2. Эффект для загрузки данных при логине или в начале (Demo Mode)
    useEffect(() => {
        const tenantId = user?.tenantId;
        
        if (tenantId) {
            // Private Mode: Закрываем модал и загружаем приватные данные
            setIsLoginModalOpen(false);
            fetchJobData(tenantId);
        } else if (!user) {
            // Demo Mode: Загружаем моковые данные при первой загрузке
            fetchJobData(null); 
        }
    }, [user, fetchJobData]);


    // --- ЛОГИКА DRAG & DROP ---

    const handleDragStart = (e, id) => {
        // ... (Code is omitted for brevity) ...
        e.dataTransfer.setData("text/plain", id);
        setDraggedJobId(id);
        e.currentTarget.classList.add('opacity-50', 'ring-2', 'ring-teal-400');
    };

    const handleDrop = (e, targetStatus) => {
        e.preventDefault();
        const droppedJobId = e.dataTransfer.getData("text/plain");

        if (!droppedJobId || droppedJobId !== draggedJobId) return;

        // ВАЖНО: В ДЕМО-РЕЖИМЕ (user=null) изменения Drag & Drop не сохраняются и не отправляются на бэкенд.
        if (!user) {
            // Просто обновляем локальное состояние в Demo Mode
             setJobs(prevJobs => prevJobs.map(job => (job.ID === droppedJobId ? { ...job, Status: targetStatus, Stage: targetStatus } : job)));
        } else {
            // Private Mode: Отправляем на Apps Script
            simulateAppsScriptUpdate(droppedJobId, targetStatus, user.tenantId);
            setJobs(prevJobs => prevJobs.map(job => (job.ID === droppedJobId ? { ...job, Status: targetStatus, Stage: targetStatus } : job)));
        }

        const draggedElement = document.querySelector(`[draggable="true"][data-id="${droppedJobId}"]`);
        if (draggedElement) draggedElement.classList.remove('opacity-50', 'ring-2', 'ring-teal-400');
        setDraggedJobId(null);
    };
    
    // Имитация вызова Apps Script API для обновления
    const simulateAppsScriptUpdate = async (id, newStatus, tenantId) => {
        console.log(`[Apps Script Simulation] Sending API call: Update job ID=${id} with Status=${newStatus} for Tenant=${tenantId}`);
        // В реальной жизни: fetch(APPS_SCRIPT_ENDPOINT, { method: 'POST', body: JSON.stringify({ action: 'updateStatus', id, newStatus, tenantId, idToken: user.idToken }) })
    };

    // --- КОНЕЦ ЛОГИКИ DRAG & DROP ---


    // Группировка процессов по статусу для Kanban
    const groupedJobs = useMemo(() => {
        if (!jobs) return {};
        return KANBAN_STAGES.reduce((acc, stage) => {
            acc[stage.id] = jobs.filter(job => job.Status === stage.id);
            return acc;
        }, {});
    }, [jobs]);


    // Обработчик клика по карточке
    const handleCardClick = (job) => {
        const fullJobDetails = MOCK_JOB_DATA.find(j => j.ID === job.ID) || job;
        setSelectedJob(fullJobDetails);
    };
    
    const handleLogout = () => {
        // Очистка сессии Google и локального состояния
        if (window.google && user && user.email) {
            window.google.accounts.id.revoke(user.email, () => {
                console.log("Google session revoked.");
                setUser(null); // Переключаемся обратно в Demo Mode
                setJobs(MOCK_JOB_DATA); // Сразу показываем Demo Data
                setError(null);
            });
        } else {
            setUser(null);
            setJobs(MOCK_JOB_DATA);
            setError(null);
        }
    };


    // Компонент стилей
    const GlobalStyles = () => (
        <style>{`
            @import url('https://fonts.googleapis.com/css2?family=Inter:wght@100..900&display=swap');
            :root { 
                font-family: 'Inter', sans-serif; 
                --color-royal-blue: #2F6BFF;
                --color-teal: #31c7b2;
            }
            .text-royal-blue { color: var(--color-royal-blue); }
            .bg-royal-blue { background-color: var(--color-royal-blue); }
            .hover\\:bg-royal-blue\\/80:hover { background-color: rgba(47, 107, 255, 0.8); }
            
            body { 
                background-color: #0c0e12; 
                color: #e2e8f0; 
                line-height: 1.6;
            }
            .scroll-smooth { scroll-behavior: smooth; }
            ::-webkit-scrollbar { width: 8px; height: 8px; }
            ::-webkit-scrollbar-track { background: #1f2937; }
            ::-webkit-scrollbar-thumb { background: #4b5563; border-radius: 4px; }
            ::-webkit-scrollbar-thumb:hover { background: #6b7280; }
        `}</style>
    );

    // --- ОСНОВНОЙ РЕНДЕРИНГ (HEADER И DASHBOARD) ---
    return (
        <div className="min-h-screen bg-gray-900 scroll-smooth">
            <GlobalStyles />
            
            {/* Панель навигации */}
            <header className="bg-gray-800 shadow-lg sticky top-0 z-20">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4 flex justify-between items-center">
                    <h1 className="text-3xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-teal-400 to-royal-blue">
                        JobHuntWOW.com
                    </h1>
                    
                    <div className="flex items-center space-x-4">
                        {!user ? (
                            <button 
                                onClick={() => setIsLoginModalOpen(true)}
                                className="px-4 py-2 text-sm font-medium rounded-lg shadow-sm text-white bg-royal-blue hover:bg-royal-blue/80 transition flex items-center"
                            >
                                <LogOut className="w-4 h-4 mr-2 rotate-180" /> Log In
                            </button>
                        ) : (
                            <div className="flex items-center space-x-4">
                                <div className="flex items-center space-x-2 text-gray-300">
                                    <img src={user.picture} alt={user.name} className="w-8 h-8 rounded-full border border-teal-400" />
                                    <span className="hidden sm:inline text-sm font-medium">{user.email}</span>
                                    <span className="text-xs font-mono px-2 py-1 bg-gray-700 rounded-lg hidden sm:inline">Tenant ID: {user.tenantId}</span>
                                </div>
                                <button 
                                    onClick={handleLogout}
                                    title="Sign out (Revokes session)"
                                    className="p-2 rounded-full text-red-400 hover:bg-gray-700 transition"
                                >
                                    <LogOut className="w-5 h-5" />
                                </button>
                            </div>
                        )}
                    </div>
                </div>
            </header>

            <main className="max-w-7xl mx-auto py-8 px-4 sm:px-6 lg:px-8">
                {error && (
                    <div className="p-4 mb-6 text-sm text-red-100 bg-red-800 rounded-lg" role="alert">
                        {error}
                    </div>
                )}
                
                {/* 1. Блок Аналитики */}
                <div className="mb-8">
                    <div className="p-4 mb-4 bg-yellow-900/50 text-yellow-300 rounded-lg text-center font-medium">
                        {user ? 'Вы в приватном режиме. Здесь только ваши данные.' : 'Демонстрационный режим. Войдите, чтобы увидеть свои процессы.'}
                    </div>
                    {loading ? (
                        <div className="text-center p-12 bg-gray-800 rounded-xl text-gray-400">Загрузка данных...</div>
                    ) : (
                        <AnalyticsDashboard jobs={jobs} />
                    )}
                </div>

                {/* 2. Kanban-доска */}
                <h2 className="text-3xl font-extrabold text-white mb-6 border-b border-gray-700 pb-2">Interview Pipeline</h2>
                
                {loading ? (
                    <div className="text-center p-12 bg-gray-800 rounded-xl text-gray-400">
                        <div className="animate-spin inline-block w-8 h-8 border-4 border-cyan-500 border-t-transparent rounded-full mr-2"></div>
                        Загрузка данных о процессах...
                    </div>
                ) : (
                    <div className="flex overflow-x-auto space-x-6 pb-4">
                        {KANBAN_STAGES.map(stage => (
                            <KanbanColumn 
                                key={stage.id} 
                                stage={stage} 
                                jobs={groupedJobs[stage.id] || []} 
                                onCardClick={handleCardClick}
                                onDragStart={handleDragStart}
                                onDrop={handleDrop}
                            />
                        ))}
                    </div>
                )}
            </main>

            <footer className="w-full text-center py-6 text-gray-500 text-sm bg-gray-800/50 mt-12">
                JobHuntWOW.com | {user ? `Tenant ID: ${user.tenantId}` : 'Demo Mode'} | Zero-Manual Open Source Tracker v1.0
            </footer>
            
            {/* DEAL DRAWER */}
            {selectedJob && (
                <OpportunityDrawer 
                    job={selectedJob} 
                    onClose={() => setSelectedJob(null)} 
                />
            )}
            
            {/* МОДАЛЬНОЕ ОКНО SSO */}
            <SSODialog
                isOpen={isLoginModalOpen}
                onClose={() => setIsLoginModalOpen(false)}
                onLoginSuccess={(userData) => { setUser(userData); setIsLoginModalOpen(false); }}
                setError={setError}
            />
        </div>
    );
}

export default App;
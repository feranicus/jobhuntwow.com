import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { LogOut, User, Send, ChevronRight, BarChart, HardHat, Star, Trash2, Link, Mail, Folder, Clock, Check, Briefcase, MapPin, DollarSign, MessageSquare, Clipboard, Calendar, X } from 'lucide-react';

// --- НАСТРОЙКА: K.I.S.S. ENDPOINT ДЛЯ GOOGLE APPS SCRIPT ---
// !!! ЭТО ВАШ БУДУЩИЙ URL РАЗВЕРНУТОГО ВЕБ-ПРИЛОЖЕНИЯ GOOGLE APPS SCRIPT !!!
// Apps Script будет читать Google Sheets, преобразовывать данные в JSON и отдавать их сюда.
const APPS_SCRIPT_ENDPOINT = "https://script.google.com/macros/s/YOUR_DEPLOYED_WEB_APP_ID/exec"; 

// --- СТАТИЧЕСКИЕ ДАННЫЕ И СТРУКТУРА ---

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

// 3. Мок-данные для детального просмотра (используются, пока нет реального API)
// В реальном приложении эти поля будут приходить из Sheets через Apps Script
const MOCK_JOB_DATA = [
    {
        ID: 'TID-12345',
        Company: 'VISA Cal',
        Role: 'Deputy CISO, EMEA',
        Stage: 'HM_INT', // Текущий активный этап для Progress Bar
        Status: 'TECHNICAL', // Статус для канбана (колонка)
        NextInterview: '2025-11-28 10:00',
        LastEmail: '2025-11-26',
        HRContact: 'Eli Cohen (eli.cohen@visa.co)',
        NextFollowupDate: '2025-12-05',
        Notes: "Need to prepare for risk management questions. Key pain point: GDPR compliance in EU.",
        ThreadURL: 'https://mail.google.com/mail/u/0/#inbox/TID-12345',
        // Дополнительные поля для Drawer
        SalaryExpectation: '250K - 280K EUR',
        Location: 'London, UK (Hybrid)',
        RecruiterCompany: 'Hays Talent',
        EmailCount: 14,
        CVVersion: 'v5.3 - Tech Focus',
        JobURL: 'https://careers.visa.com/job/deputy-ciso-emea',
        IsFavorite: true,
        ActivityFeed: [
            { date: '2025-11-26', type: 'Email', description: 'Recruiter asked for next availability (Thread updated)' },
            { date: '2025-11-20', type: 'Interview', description: 'Hiring Manager (Mr. John Smith) round completed. Feedback requested.' },
            { date: '2025-11-14', type: 'Interview', description: 'Technical Interview #1 (Architectural Design) completed. Score 8/10.' },
            { date: '2025-11-10', type: 'Interview', description: 'First Interview with Lead Recruiter (Sarah Connor).' },
            { date: '2025-11-06', type: 'System', description: 'HR screen confirmed. Next step: First Interview.' },
            { date: '2025-11-05', type: 'Applied', description: 'Application submitted via LinkedIn. System created tracking card.' },
        ],
        Insights: {
            probability: 'High (85%)',
            followupRec: 'None needed. Next step depends on company action.',
            toneAnalysis: 'Professional and warm tone. Process moving at normal pace (T+15 days).'
        }
    },
    // ... другие моковые вакансии
    { ID: 'TID-67890', Company: 'Google', Role: 'Principal Software Engineer', Stage: 'HR_SCREEN', Status: 'APPLIED', NextInterview: null, LastEmail: '2025-11-15', HRContact: 'Jane Doe', NextFollowupDate: '2025-11-20', ThreadURL: '#', SalaryExpectation: '300K+ USD', Location: 'Mountain View, CA (Onsite)', RecruiterCompany: 'Internal HR', EmailCount: 3, CVVersion: 'v5.4 - SWE Focus', IsFavorite: false, ActivityFeed: [{ date: '2025-11-15', type: 'Applied', description: 'Application submitted.' }], Insights: { probability: 'Medium (50%)', followupRec: 'Send follow-up email today.', toneAnalysis: 'Neutral, standard automated reply.' } },
    { ID: 'TID-99999', Company: 'SpaceX', Role: 'Flight Software Engineer', Stage: 'FIRST_INT', Status: 'HR_SCREEN', NextInterview: '2025-12-10 14:00', LastEmail: '2025-11-01', HRContact: 'Elon Musk', NextFollowupDate: '2025-12-01', ThreadURL: '#', SalaryExpectation: '180K - 220K USD', Location: 'Hawthorne, CA (Onsite)', RecruiterCompany: 'Aerospace Inc.', EmailCount: 8, CVVersion: 'v5.3 - Tech Focus', IsFavorite: true, ActivityFeed: [{ date: '2025-11-01', type: 'Email', description: 'Interview invitation received.' }], Insights: { probability: 'Medium (60%)', followupRec: 'Follow up is overdue! Send immediately.', toneAnalysis: 'Very formal and direct tone.' } }
];

// 4. Иконка для отображения статуса (используется в KanbanCard)
const StatusIcon = ({ status }) => {
    switch (status) {
        case 'HR_SCREEN': return <User className="w-4 h-4 text-blue-300" />;
        case 'FIRST_INT': return <ChevronRight className="w-4 h-4 text-cyan-300" />;
        case 'TECH_INT': return <HardHat className="w-4 h-4 text-amber-300" />;
        case 'HM_INT': return <Briefcase className="w-4 h-4 text-purple-300" />;
        case 'PANEL_INT': return <MessageSquare className="w-4 h-4 text-pink-300" />;
        case 'OFFER': return <Check className="w-4 h-4 text-green-300" />;
        case 'REJECTED': return <LogOut className="w-4 h-4 text-red-300" />;
        case 'APPLIED':
        default: return <Send className="w-4 h-4 text-indigo-300" />;
    }
}


// --- КОМПОНЕНТЫ ДЕТАЛЬНОГО ПРОСМОТРА (OPPORTUNITY DRAWER) ---

/**
 * Горизонтальный прогресс-бар в стиле Pipedrive.
 */
const OpportunityProgressBar = ({ currentStage }) => {
    // Находим индекс текущего активного этапа
    const currentIndex = PROGRESS_STAGES.findIndex(s => s.id === currentStage);
    
    return (
        <div className="flex justify-between items-start text-sm mt-4 mb-8 relative">
            {/* Линия прогресса */}
            <div className="absolute top-1/2 left-0 right-0 h-1 bg-gray-700/50 -translate-y-1/2 mx-8 z-0">
                <div 
                    className="h-1 bg-gradient-to-r from-teal-400 to-green-500 rounded-full transition-all duration-700"
                    style={{ width: `${(currentIndex / (PROGRESS_STAGES.length - 1)) * 100}%` }}
                />
            </div>

            {PROGRESS_STAGES.map((stage, index) => {
                const isCompleted = index < currentIndex; // Завершено: до текущего индекса
                const isActive = index === currentIndex; // Активно: текущий индекс

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


/**
 * Компонент вертикальной временной шкалы
 */
const OpportunityTimeline = ({ activities }) => (
    <div className="space-y-6 pt-2">
        {activities.map((activity, index) => {
            const isLatest = index === 0;
            const Icon = activity.type === 'Interview' ? Calendar : (activity.type === 'Email' ? Mail : Clock);
            
            return (
                <div key={index} className="flex relative pl-4">
                    {/* Вертикальная линия */}
                    {index < activities.length - 1 && (
                        <div className={`absolute left-0 top-6 bottom-0 w-0.5 ${isLatest ? 'bg-teal-500/50' : 'bg-gray-700'}`}></div>
                    )}
                    
                    {/* Точка */}
                    <div className={`absolute left-0 top-0 w-4 h-4 rounded-full ${isLatest ? 'bg-teal-500 shadow-teal-500/50 shadow-md' : 'bg-gray-600'} -translate-x-1/2`}></div>

                    {/* Содержимое */}
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


/**
 * Детальный боковой Drawer для просмотра процесса.
 */
const OpportunityDrawer = ({ job, onClose }) => {
    // Заглушка для получения логотипа компании (Clearbit)
    const companyLogoUrl = `https://logo.clearbit.com/${job.Company.toLowerCase().replace(/[^a-z0-9.]/g, '')}.com?size=50`;

    const [activeTab, setActiveTab] = useState('Activity');
    
    // Определяем текущий активный этап (Stage) для Progress Bar
    const currentStageId = job.Stage; 

    // Используем моковые данные для Activity/Notes/Insights, пока Apps Script их не отдаст
    // В реальной жизни job уже должен содержать эти данные
    const mockDetails = MOCK_JOB_DATA.find(j => j.ID === job.ID) || MOCK_JOB_DATA[0];

    // Функция для рендеринга иконок в Summary
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
            {/* Overlay для закрытия при клике вне Drawer'а */}
            <div 
                className="absolute inset-0 bg-black/50 transition-opacity duration-300"
                onClick={onClose}
            ></div>
            
            {/* Сам Drawer (правая панель) */}
            <div className="fixed inset-y-0 right-0 max-w-full flex">
                <div className="w-screen max-w-3xl bg-gray-900 border-l border-gray-700 shadow-2xl overflow-y-auto">
                    
                    {/* HEADER */}
                    <div className="sticky top-0 bg-gray-900 z-10 p-6 border-b border-gray-800">
                        <div className="flex justify-between items-center mb-4">
                            <div className="flex items-center space-x-4">
                                {/* Логотип компании */}
                                <img 
                                    src={companyLogoUrl} 
                                    alt={`${job.Company} logo`} 
                                    className="w-10 h-10 rounded-lg border border-gray-700 object-contain bg-white p-1"
                                    onError={(e) => { e.target.onerror = null; e.target.src="https://placehold.co/50x50/374151/f9fafb?text=Co"; }}
                                />
                                <div>
                                    <h2 className="text-2xl font-bold text-white">{job.Company}</h2>
                                    <p className="text-md text-gray-400">{job.Role}</p>
                                </div>
                            </div>
                            <div className="flex items-center space-x-2">
                                {/* Кнопки действий */}
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
                        
                        {/* PROGRESS BAR */}
                        <OpportunityProgressBar currentStage={currentStageId} />
                    </div>

                    {/* CONTENT AREA */}
                    <div className="p-6 space-y-8">
                        
                        {/* A. SUMMARY SECTION */}
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

                        {/* B. INSIGHTS SECTION */}
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

                        {/* C. ACTIVITY FEED (Tabs) */}
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


// --- КОМПОНЕНТЫ КАНБАНА ---

/**
 * Компонент карточки процесса в Kanban
 */
const KanbanCard = ({ job, onClick, onDragStart }) => {
    const statusMeta = KANBAN_STAGES.find(s => s.id === job.Status) || KANBAN_STAGES[0];
    const isActionRequired = job.NextFollowupDate && (new Date(job.NextFollowupDate) < new Date());
    
    // В MVP это просто заглушка
    // const handleStatusChange = (newStatus) => {
    //     console.log(`[ACTION] Trying to update job ${job.ID} to ${newStatus}. Requires Apps Script POST handler.`);
    // };

    // Заглушка для получения логотипа компании
    const companyLogoUrl = `https://logo.clearbit.com/${job.Company.toLowerCase().replace(/[^a-z0-9.]/g, '')}.com?size=30`;


    return (
        <div 
            className="bg-gray-800 rounded-lg p-4 shadow-xl border-t-2 border-b-2 border-gray-700 hover:shadow-2xl transition duration-300 ease-in-out transform hover:-translate-y-0.5 cursor-pointer"
            onClick={() => onClick(job)} // Передаем клик для открытия Drawer'а
            draggable="true" // Включаем возможность перетаскивания
            onDragStart={(e) => onDragStart(e, job.ID)} // Добавляем обработчик начала перетаскивания
        >
            <div className="flex justify-between items-start mb-2">
                <div className="flex items-center space-x-2">
                    <img 
                        src={companyLogoUrl} 
                        alt="" 
                        className="w-6 h-6 rounded border border-gray-700 object-contain bg-white"
                        onError={(e) => { e.target.onerror = null; e.target.src="https://placehold.co/30x30/374151/f9fafb?text=C"; }}
                    />
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

/**
 * Компонент Kanban-колонки
 */
const KanbanColumn = ({ stage, jobs, onCardClick, onDragStart, onDrop }) => {
    
    const handleDragOver = (e) => {
        e.preventDefault(); // Обязательно для разрешения события onDrop
        e.currentTarget.classList.add('bg-gray-700/70', 'ring-2', 'ring-teal-400');
    };

    const handleDragLeave = (e) => {
        e.currentTarget.classList.remove('bg-gray-700/70', 'ring-2', 'ring-teal-400');
    };

    const handleOnDrop = (e) => {
        e.currentTarget.classList.remove('bg-gray-700/70', 'ring-2', 'ring-teal-400');
        onDrop(e, stage.id); // Вызываем обработчик drop в App
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


/**
 * Компонент панели аналитики (График)
 */
const AnalyticsDashboard = ({ jobs }) => {
    // Используем KANBAN_STAGES для подсчета
    const totalJobs = jobs.length;
    
    // Расчет статистики
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

    // Placeholder для простого отображения без библиотеки Recharts
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


// --- ГЛАВНЫЙ КОМПОНЕНТ ПРИЛОЖЕНИЯ ---

function App() {
    // Временно используем мок-данные, пока Apps Script не будет настроен. 
    // В реальном приложении эта строка будет: const [jobs, setJobs] = useState([]); 
    const [jobs, setJobs] = useState(MOCK_JOB_DATA); 
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);
    const [selectedJob, setSelectedJob] = useState(null);
    
    // Переменная для хранения ID перетаскиваемой карточки
    const [draggedJobId, setDraggedJobId] = useState(null);

    // 1. Хук для получения данных из Apps Script Web App (оставлен для будущей интеграции)
    const fetchJobData = useCallback(async () => {
        setLoading(true);
        setError(null);
        try {
            // !!! В MVP используется MOCK_JOB_DATA. Когда Apps Script будет готов, 
            // замените блок ниже на реальный fetch !!!
            // const response = await fetch(APPS_SCRIPT_ENDPOINT); 
            // ...
            
            // Заглушка, чтобы не терять D&D изменения в UI:
            if (jobs.length === 0 && MOCK_JOB_DATA.length > 0) {
                 setJobs(MOCK_JOB_DATA);
            }

        } catch (e) {
            console.error("Error fetching job data from Apps Script:", e);
            setError(`Failed to load data. Check APPS_SCRIPT_ENDPOINT. Error: ${e.message}`);
        } finally {
            setLoading(false);
        }
    }, [jobs.length]); // Добавляем jobs.length в зависимости, чтобы избежать бесконечного цикла и обновлять, если список пуст

    // 2. Запуск загрузки данных при монтировании компонента
    useEffect(() => {
        // Мы не запускаем fetchJobData, чтобы не перезатирать D&D изменения мок-данными
        // Вместо этого, просто вызываем fetchJobData, если список jobs пуст
        if (jobs.length === 0) {
            fetchJobData();
        }
        
        // Опционально: автоматическое обновление (раз в 60 сек, пока отключено)
        // const intervalId = setInterval(fetchJobData, 60000); 
        // return () => clearInterval(intervalId);
    }, [jobs.length, fetchJobData]);


    // --- ЛОГИКА DRAG & DROP ---

    const handleDragStart = (e, id) => {
        e.dataTransfer.setData("text/plain", id);
        setDraggedJobId(id);
        // Добавляем класс для визуального эффекта перетаскивания (необязательно, но приятно)
        e.currentTarget.classList.add('opacity-50', 'ring-2', 'ring-teal-400');
    };

    const handleDrop = (e, targetStatus) => {
        e.preventDefault();
        const droppedJobId = e.dataTransfer.getData("text/plain");

        if (!droppedJobId || droppedJobId !== draggedJobId) return;

        // Удаляем классы перетаскивания
        const draggedElement = document.querySelector(`[draggable="true"][data-id="${droppedJobId}"]`);
        if (draggedElement) {
            draggedElement.classList.remove('opacity-50', 'ring-2', 'ring-teal-400');
        }

        // Обновление состояния в React
        setJobs(prevJobs => {
            const updatedJobs = prevJobs.map(job => {
                if (job.ID === droppedJobId) {
                    console.log(`Job ${droppedJobId} moved to ${targetStatus}`);
                    
                    // !!! ВАЖНО: В реальной версии здесь будет ВЫЗОВ Apps Script API (метод POST/PUT)
                    simulateAppsScriptUpdate(droppedJobId, targetStatus);
                    
                    // Обновляем Status (колонка) и Stage (прогресс-бар)
                    return { 
                        ...job, 
                        Status: targetStatus,
                        Stage: targetStatus, // Для MVP Status = Stage для простоты
                    };
                }
                return job;
            });
            return updatedJobs;
        });
        
        setDraggedJobId(null);
    };
    
    // Имитация вызова Apps Script API для обновления
    const simulateAppsScriptUpdate = async (id, newStatus) => {
        console.log(`[Apps Script Simulation] Sending API call: Update job ID=${id} with Status=${newStatus}`);
        
        // !!! ЭТОТ БЛОК ДОЛЖЕН БЫТЬ РЕАЛИЗОВАН ПРИ НАСТРОЙКЕ APPS SCRIPT !!!
        // try {
        //     const response = await fetch(APPS_SCRIPT_ENDPOINT, { 
        //         method: 'POST', 
        //         headers: { 'Content-Type': 'application/json' },
        //         body: JSON.stringify({ action: 'updateStatus', id, newStatus }) 
        //     });
        //     const result = await response.json();
        //     if (!result.success) {
        //         console.error("API update failed:", result.error);
        //     }
        // } catch (e) {
        //     console.error("Network error during Apps Script update:", e);
        // }
    };

    // --- КОНЕЦ ЛОГИКИ DRAG & DROP ---


    // Группировка процессов по статусу для Kanban
    const groupedJobs = useMemo(() => {
        return KANBAN_STAGES.reduce((acc, stage) => {
            // Ищем по Status, а не Stage, так как Status - это ID колонки
            acc[stage.id] = jobs.filter(job => job.Status === stage.id);
            return acc;
        }, {});
    }, [jobs]);


    // Обработчик клика по карточке
    const handleCardClick = (job) => {
        // Находим полную информацию о процессе для детального просмотра
        const fullJobDetails = MOCK_JOB_DATA.find(j => j.ID === job.ID) || job;
        setSelectedJob(fullJobDetails);
    };

    // Компонент стилей (включаем Tailwind CSS + кастомные цвета)
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

    // Основной рендер
    return (
        <div className="min-h-screen bg-gray-900 scroll-smooth">
            <GlobalStyles />
            
            {/* Панель навигации */}
            <header className="bg-gray-800 shadow-lg sticky top-0 z-20">
                <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4 flex justify-between items-center">
                    <h1 className="text-3xl font-extrabold text-transparent bg-clip-text bg-gradient-to-r from-teal-400 to-royal-blue">
                        JobHuntWOW.com
                    </h1>
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
                                onCardClick={handleCardClick} // Передаем обработчик клика
                                onDragStart={handleDragStart} // Передаем обработчик начала перетаскивания
                                onDrop={handleDrop} // Передаем обработчик drop
                            />
                        ))}
                    </div>
                )}
            </main>

            <footer className="w-full text-center py-6 text-gray-500 text-sm bg-gray-800/50 mt-12">
                JobHuntWOW.com | Zero-Manual Open Source Tracker v1.0
            </footer>
            
            {/* DEAL DRAWER */}
            {selectedJob && (
                <OpportunityDrawer 
                    job={selectedJob} 
                    onClose={() => setSelectedJob(null)} 
                />
            )}
        </div>
    );
}

export default App;
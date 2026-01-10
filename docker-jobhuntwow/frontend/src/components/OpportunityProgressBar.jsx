import React from 'react';
import { User, ChevronRight, HardHat, Briefcase, MessageSquare, Check } from 'lucide-react';

const PROGRESS_STAGES = [
    { id: 'HR_SCREEN', name: 'HR', icon: User },
    { id: 'FIRST_INT', name: '1st', icon: ChevronRight },
    { id: 'TECH_INT', name: 'Tech #1', icon: HardHat },
    { id: 'HM_INT', name: 'Hiring Mngr', icon: Briefcase },
    { id: 'PANEL_INT', name: 'Panel', icon: MessageSquare },
    { id: 'OFFER', name: 'Offer', icon: Check },
];

const OpportunityProgressBar = ({ currentStage }) => { 
    const currentIndex = PROGRESS_STAGES.findIndex(s => s.id === currentStage);
    return (
        <div className="flex justify-between items-start text-sm mt-4 mb-8 relative">
            <div className="absolute top-1/2 left-0 right-0 h-1 bg-gray-700/50 -translate-y-1/2 mx-8">
                <div className="h-1 bg-teal-400 transition-all duration-700"
                     style={{ width: `${(currentIndex / (PROGRESS_STAGES.length - 1)) * 100}%` }} />
            </div>
            {PROGRESS_STAGES.map((stage, index) => {
                const isCompleted = index < currentIndex;
                const isActive = index === currentIndex;
                return (
                    <div key={stage.id} className="flex flex-col items-center w-1/6 z-10">
                        <div className={`p-2 rounded-full border-4 ${isCompleted ? 'bg-green-500 border-green-700' : isActive ? 'bg-amber-500 border-amber-700' : 'bg-gray-800 border-gray-600'}`}>
                            {isCompleted ? <Check className="w-4 h-4 text-white" /> : <stage.icon className="w-4 h-4 text-white" />}
                        </div>
                        <span className="mt-2 text-[10px] text-gray-400 text-center">{stage.name}</span>
                    </div>
                );
            })}
        </div>
    );
};

export default OpportunityProgressBar;
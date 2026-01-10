import React from 'react';
import { Calendar, User } from 'lucide-react';

const KanbanCard = ({ job, onClick, onDragStart }) => { 
    const isActionRequired = job.NextFollowupDate && (new Date(job.NextFollowupDate) < new Date());
    const companyLogoUrl = `https://logo.clearbit.com/${job.Company.toLowerCase().replace(/[^a-z0-9.]/g, '')}.com?size=30`;
    
    return (
        <div 
            className="bg-gray-800 rounded-lg p-4 shadow-xl border-t-2 border-b-2 border-gray-700 hover:shadow-2xl transition cursor-pointer"
            onClick={() => onClick(job)}
            draggable="true"
            onDragStart={(e) => onDragStart(e, job.ID)}
            data-id={job.ID}
        >
            <div className="flex justify-between items-start mb-2">
                <div className="flex items-center space-x-2">
                    <img src={companyLogoUrl} alt="" className="w-6 h-6 rounded bg-white p-0.5" 
                         onError={(e) => { e.target.src="https://placehold.co/30x30/374151/f9fafb?text=C"; }} />
                    <div className="text-lg font-bold text-white truncate">{job.Company}</div>
                </div>
            </div>
            
            <p className="text-sm text-gray-400 mb-3 truncate font-medium">{job.Role || 'N/A Role'}</p>

            <div className="space-y-2 text-xs">
                {job.NextInterview && (
                    <div className="flex items-center text-yellow-300">
                        <Calendar className="w-4 h-4 mr-2" />
                        <span>Interview: <span className="font-semibold">{job.NextInterview}</span></span>
                    </div>
                )}
                <div className="flex items-center text-gray-400">
                    <User className="w-4 h-4 mr-2" />
                    <span>HR: <span className="font-semibold">{job.HRContact || 'N/A'}</span></span>
                </div>
            </div>

            {isActionRequired && (
                <div className="mt-3 p-2 text-xs font-medium text-amber-900 bg-amber-400 rounded-md">
                    ⚠️ Follow-up Due: {job.NextFollowupDate}
                </div>
            )}
        </div>
    );
};

export default KanbanCard;
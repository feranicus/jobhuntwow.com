import React from 'react';
import { Calendar, Mail, Clock } from 'lucide-react';

const OpportunityTimeline = ({ activities }) => (
    <div className="space-y-6 pt-2">
        {activities.map((activity, index) => {
            const isLatest = index === 0;
            return (
                <div key={index} className="flex relative pl-4">
                    {index < activities.length - 1 && (
                        <div className="absolute left-0 top-6 bottom-0 w-0.5 bg-gray-700"></div>
                    )}
                    <div className={`absolute left-0 top-0 w-4 h-4 rounded-full ${isLatest ? 'bg-teal-500 shadow-md' : 'bg-gray-600'} -translate-x-1/2`}></div>
                    <div className="ml-4 flex-1 pb-4">
                        <p className={`text-sm font-semibold ${isLatest ? 'text-white' : 'text-gray-300'}`}>
                            {activity.description}
                        </p>
                        <p className="text-xs text-gray-500 mt-1 flex items-center">
                            <Clock className="w-3 h-3 mr-1" /> {activity.date}
                            <span className="ml-2 px-2 py-0.5 bg-indigo-800/50 text-indigo-300 rounded-full">{activity.type}</span>
                        </p>
                    </div>
                </div>
            );
        })}
    </div>
);

export default OpportunityTimeline;
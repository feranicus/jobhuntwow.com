/**
 * JobHuntWOW: Google Apps Script Backend (API Proxy + Automation)
 *
 * This script serves three purposes:
 * 1. doGet(e): Provides the entire dataset (JSON) to the React frontend.
 * 2. doPost(e): Handles commands from React (e.g., status updates from Drag & Drop).
 * 3. AutomationTask(): The scheduled CRON job to parse Gmail and update Google Sheets.
 */

// --- КОНСТАНТЫ И НАСТРОЙКИ ---
// !!! ОБЯЗАТЕЛЬНО ЗАМЕНИТЕ ЭТО НА ID ВАШЕЙ ТАБЛИЦЫ GOOGLE SHEETS !!!
const SPREADSHEET_ID = 'YOUR_GOOGLE_SHEET_ID_HERE'; 
const SHEET_NAME = 'PipelineData';

// Заголовки столбцов (должны совпадать со схемой Sheets)
const HEADERS = [
    "ID", "Company", "Role", "Stage", "Status", "NextInterview", 
    "LastEmail", "HRContact", "NextFollowup", "ThreadURL", 
    "IsFavorite", "Notes", "SalaryExpectation", "Location", 
    "RecruiterCompany", "CVVersion", "JobURL", "ActivityFeed", "Insights"
];

// --- Вспомогательные функции Sheets ---

/**
 * Открывает лист и возвращает его.
 * @returns {GoogleAppsScript.Spreadsheet.Sheet}
 */
function getSheet() {
    try {
        const sheet = SpreadsheetApp.openById(SPREADSHEET_ID).getSheetByName(SHEET_NAME);
        if (!sheet) throw new Error(`Sheet "${SHEET_NAME}" not found.`);
        return sheet;
    } catch (e) {
        Logger.log('Error getting sheet: ' + e.message);
        return null;
    }
}

/**
 * Находит индекс строки по ID процесса.
 * @param {string} id Thread ID для поиска.
 * @param {GoogleAppsScript.Spreadsheet.Sheet} sheet 
 * @returns {number} Индекс строки (начиная с 1) или -1, если не найдено.
 */
function findRowIndex(id, sheet) {
    // Ищем в первом столбце (A)
    const textFinder = sheet.getRange('A:A').createTextFinder(id);
    const result = textFinder.matchEntireCell(true).findNext();
    return result ? result.getRow() : -1;
}

// --- 1. API PROXY ДЛЯ REACT FRONTEND (doGet) ---

/**
 * Обрабатывает GET-запросы от React.
 * Считывает все данные и возвращает их в формате JSON.
 * @param {GoogleAppsScript.Events.DoGet} e
 */
function doGet(e) {
    const sheet = getSheet();
    if (!sheet) return createJsonResponse({ error: 'Database access failed.' }, 500);

    const range = sheet.getDataRange();
    const values = range.getValues();
    
    // Если есть только строка заголовка или нет данных
    if (values.length <= 1) return createJsonResponse([]);

    const data = values.slice(1);
    const headerRow = values[0];

    // Преобразуем массив массивов в массив объектов
    const jsonArray = data.map(row => {
        const item = {};
        headerRow.forEach((header, index) => {
            let value = row[index];
            if (value instanceof Date) {
                value = value.toISOString().slice(0, 10); // YYYY-MM-DD для React
            }
            item[header] = value;
        });
        return item;
    });

    return createJsonResponse(jsonArray);
}

// --- 2. API ENDPOINT ДЛЯ REACT (doPost) ---

/**
 * Обрабатывает POST-запросы от React (например, Drag & Drop).
 * @param {GoogleAppsScript.Events.DoPost} e
 */
function doPost(e) {
    try {
        const sheet = getSheet();
        if (!sheet) throw new Error('Sheet unavailable.');

        const requestBody = JSON.parse(e.postData.contents);
        const { action, id, newStatus, isFavorite } = requestBody;

        if (action === 'updateStatus') {
            const rowIndex = findRowIndex(id, sheet);
            if (rowIndex === -1) throw new Error(`Job ID ${id} not found.`);
            
            // Находим индексы столбцов Status и Stage
            const statusIndex = HEADERS.indexOf('Status');
            const stageIndex = HEADERS.indexOf('Stage');
            
            // Получаем диапазон, который нужно обновить (только Status и Stage)
            // Столбец Status находится на 5-й позиции (индекс 4)
            const statusColIndex = 1 + statusIndex; 
            const stageColIndex = 1 + stageIndex; 
            
            // Обновляем Status (колонка Kanban)
            sheet.getRange(rowIndex, statusColIndex).setValue(newStatus);
            // Обновляем Stage (прогресс-бар). Для D&D используем тот же статус.
            sheet.getRange(rowIndex, stageColIndex).setValue(newStatus);

            return createJsonResponse({ success: true, message: `Status updated to ${newStatus}` });
        } 
        
        else if (action === 'updateFavorite') {
            const rowIndex = findRowIndex(id, sheet);
            if (rowIndex === -1) throw new Error(`Job ID ${id} not found.`);
            
            const favoriteIndex = HEADERS.indexOf('IsFavorite');
            const favoriteColIndex = 1 + favoriteIndex;
            
            sheet.getRange(rowIndex, favoriteColIndex).setValue(isFavorite);
            
            return createJsonResponse({ success: true, message: `Favorite status set to ${isFavorite}` });
        }
        
        throw new Error('Invalid action provided.');

    } catch (error) {
        Logger.log('doPost Error: ' + error.message);
        return createJsonResponse({ success: false, error: error.message }, 400);
    }
}

// --- Вспомогательная функция JSON-ответа ---

/**
 * Создает стандартизированный JSON-ответ с CORS-заголовками.
 * @param {Object} data Объект или массив для сериализации в JSON.
 * @param {number} status HTTP статус (для логирования, Web App всегда возвращает 200).
 * @returns {GoogleAppsScript.Content.TextOutput}
 */
function createJsonResponse(data, status = 200) {
    const output = ContentService.createTextOutput(JSON.stringify(data));
    output.setMimeType(ContentService.MimeType.JSON);
    output.setHeader('Access-Control-Allow-Origin', '*'); 
    output.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    output.setHeader('Access-Control-Allow-Headers', 'Content-Type');
    return output;
}

/**
 * Обрабатывает OPTIONS-запрос (для CORS preflight).
 */
function doOptions() {
    return createJsonResponse({});
}

// --- 3. GMAIL AUTOMATION TASK (CRON Trigger) ---

/**
 * Основная функция, которая запускается по CRON-триггеру (каждые 15 минут).
 */
function AutomationTask() {
    const sheet = getSheet();
    if (!sheet) return;

    // Получаем существующие ID из Sheets, чтобы избежать дублирования и для обновления
    const allData = sheet.getDataRange().getValues();
    const existingIds = allData.length > 1 ? allData.slice(1).map(row => row[0]) : [];

    // Ищем ветки за последние 30 дней, которые не прочитаны И содержат ключевые слова
    const query = 'subject:("interview" OR "next steps" OR "schedule" OR "offer" OR "feedback") is:unread newer_than:30d';
    const threads = GmailApp.search(query, 0, 50); 
    
    threads.forEach(thread => {
        const id = thread.getId();
        
        if (existingIds.includes(id)) {
            // ОБНОВЛЕНИЕ СУЩЕСТВУЮЩЕГО ПРОЦЕССА
            updateExistingJob(thread, id, sheet, allData);
        } else {
            // СОЗДАНИЕ НОВОГО ПРОЦЕССА
            createNewJob(thread, sheet);
        }
        thread.markRead(); // Помечаем как прочитанное
    });
    
    // Пересчитываем NextFollowup для всех активных процессов
    generateFollowUpReminders(sheet);
}

/**
 * Парсит ветку и создает новую строку в Google Sheets.
 */
function createNewJob(thread, sheet) {
    const message = thread.getMessages().pop(); // Берем последнее сообщение
    const subject = thread.getFirstMessageSubject();
    
    const parsed = parseEmail(message, subject);

    // Логика начального определения статуса
    let status = 'APPLIED';
    let stage = 'APPLIED';
    if (parsed.nextInterview || subject.toLowerCase().includes('schedule') || subject.toLowerCase().includes('hr')) {
        status = 'HR_SCREEN'; 
        stage = 'HR_SCREEN';
    } else if (subject.toLowerCase().includes('offer')) {
        status = 'OFFER';
        stage = 'OFFER';
    }

    const newRow = HEADERS.map(header => {
        switch (header) {
            case 'ID': return thread.getId();
            case 'Company': return parsed.company;
            case 'Role': return parsed.role;
            case 'Stage': return stage; 
            case 'Status': return status;
            case 'NextInterview': return parsed.nextInterview || '';
            case 'LastEmail': return message.getDate().toISOString().slice(0, 10);
            case 'HRContact': return parsed.hrContact;
            case 'ThreadURL': return `https://mail.google.com/mail/u/0/#inbox/${thread.getId()}`;
            case 'IsFavorite': return false;
            case 'Notes': return `[Initial Mail from: ${parsed.hrContact}]\n`;
            case 'ActivityFeed':
                const initialActivity = [{ date: new Date().toISOString().slice(0, 10), type: 'System', description: 'Tracking started.' }];
                return JSON.stringify(initialActivity);
            case 'Insights': return JSON.stringify({ probability: 'Unknown', followupRec: 'Wait for response.', toneAnalysis: 'Neutral' });
            // Ручные поля оставляем пустыми
            default: return ''; 
        }
    });

    sheet.appendRow(newRow);
}

/**
 * Обновляет существующую строку, если найдено новое письмо в ветке.
 */
function updateExistingJob(thread, id, sheet, allData) {
    const rowIndex = findRowIndex(id, sheet); 
    if (rowIndex === -1) return;

    const rowRange = sheet.getRange(rowIndex, 1, 1, HEADERS.length);
    const currentValues = rowRange.getValues()[0]; 
    
    const lastEmailIndex = HEADERS.indexOf('LastEmail');
    const activityIndex = HEADERS.indexOf('ActivityFeed');

    // Находим дату последнего обработанного email
    const lastProcessedEmailDate = new Date(currentValues[lastEmailIndex]);
    
    // Фильтруем только новые сообщения, пришедшие после LastEmail
    const messages = thread.getMessages().filter(m => m.getDate().getTime() > lastProcessedEmailDate.getTime());

    if (messages.length > 0) {
        let needsUpdate = false;
        
        messages.forEach(message => {
            const parsed = parseEmail(message, thread.getFirstMessageSubject());
            
            // 1. Обновляем LastEmail
            currentValues[lastEmailIndex] = message.getDate().toISOString().slice(0, 10);
            needsUpdate = true;
            
            // 2. Обновляем NextInterview, если найден новый
            const nextInterviewIndex = HEADERS.indexOf('NextInterview');
            if (parsed.nextInterview) {
                currentValues[nextInterviewIndex] = parsed.nextInterview;
                needsUpdate = true;
            }

            // 3. Обновляем Activity Feed
            let activities = [];
            try {
                // Если ActivityFeed не пустой (т.е. содержит JSON строку), парсим ее. Иначе инициализируем пустым массивом.
                activities = currentValues[activityIndex] ? JSON.parse(currentValues[activityIndex]) : [];
            } catch (e) {
                Logger.log('Failed to parse ActivityFeed for update: ' + e.message);
                activities = [];
            }
            
            const newActivity = { 
                date: message.getDate().toISOString().slice(0, 10), 
                type: parsed.activityType, 
                description: parsed.activityDescription 
            };
            activities.unshift(newActivity); // Добавляем в начало
            currentValues[activityIndex] = JSON.stringify(activities);
            needsUpdate = true;


            // 4. Обновляем Status/Stage по ключевым словам в новом письме (простая эвристика)
            const statusIndex = HEADERS.indexOf('Status');
            const stageIndex = HEADERS.indexOf('Stage');
            const subjectLower = message.getSubject().toLowerCase();
            
            if (subjectLower.includes('technical') && currentValues[statusIndex] === 'HR_SCREEN') {
                 currentValues[statusIndex] = 'TECH_INT';
                 currentValues[stageIndex] = 'TECH_INT';
                 needsUpdate = true;
            } else if (subjectLower.includes('final round') && currentValues[statusIndex] === 'TECH_INT') {
                 currentValues[statusIndex] = 'HM_INT';
                 currentValues[stageIndex] = 'HM_INT';
                 needsUpdate = true;
            } else if (subjectLower.includes('offer')) {
                 currentValues[statusIndex] = 'OFFER';
                 currentValues[stageIndex] = 'OFFER';
                 needsUpdate = true;
            } else if (subjectLower.includes('unsuccessful') || subjectLower.includes('reject')) {
                 currentValues[statusIndex] = 'REJECTED';
                 currentValues[stageIndex] = 'REJECTED';
                 needsUpdate = true;
            }
        });
        
        if (needsUpdate) {
             // Записываем обновленные данные обратно в лист
             rowRange.setValues([currentValues]);
        }
    }
}

/**
 * Парсер-заглушка для извлечения ключевых данных из сообщения. 
 * В v1 это RegEx + простая эвристика.
 */
function parseEmail(message, subject) {
    const body = message.getPlainBody().toLowerCase();
    
    // 1. Поиск даты интервью
    const dateMatch = body.match(/(\d{1,2}[\/\-]\d{1,2}[\/\-]\d{4}|\w{3,4}\s+\d{1,2}(?:st|nd|rd|th)?,\s+\d{4})/i);
    const timeMatch = body.match(/(\d{1,2}:\d{2}\s*(?:am|pm)?)/i);
    let nextInterview = null;
    if (dateMatch) {
        let datePart = dateMatch[0].trim();
        let timePart = timeMatch ? timeMatch[0].trim() : '';
        nextInterview = datePart + (timePart ? ' ' + timePart : '');
    }
    
    // 2. Поиск имени HR (очень простая эвристика)
    const hrMatch = body.match(/(dear|hi|hello)\s+([\w\s]+?),/i);
    const hrContact = hrMatch ? hrMatch[2].trim() : (message.getFrom().split('<')[0].trim() || 'Unknown HR');
    
    // 3. Поиск компании/роли (на основе темы)
    const subjectParts = subject.split(/[\-\|:]/);
    const company = subjectParts[0].trim() || 'Unknown Company';
    const role = subjectParts.length > 1 ? subjectParts[1].trim() : 'Unspecified Role';

    // 4. Определение типа активности и описания
    let activityType = 'Email';
    let activityDescription = `New email received: "${message.getSubject().substring(0, 50)}..."`;

    if (subject.toLowerCase().includes('interview') || subject.toLowerCase().includes('schedule')) {
        activityType = 'Interview';
        activityDescription = nextInterview ? `Interview scheduled for ${nextInterview}` : 'Interview scheduling email received.';
    } else if (subject.toLowerCase().includes('offer')) {
        activityType = 'Offer';
        activityDescription = 'Offer documents received!';
    } else if (subject.toLowerCase().includes('rejection') || subject.toLowerCase().includes('unsuccessful')) {
        activityType = 'System';
        activityDescription = 'Rejection email received.';
    }

    return {
        company: company,
        role: role,
        nextInterview: nextInterview,
        hrContact: hrContact,
        activityType: activityType,
        activityDescription: activityDescription,
    };
}

/**
 * Пересчитывает NextFollowup для активных процессов.
 */
function generateFollowUpReminders(sheet) {
    const dataRange = sheet.getDataRange();
    const data = dataRange.getValues();
    
    if (data.length <= 1) return;

    const lastEmailIndex = HEADERS.indexOf('LastEmail');
    const statusIndex = HEADERS.indexOf('Status');
    const followUpIndex = HEADERS.indexOf('NextFollowup');

    for (let i = 1; i < data.length; i++) {
        const row = data[i];
        const status = row[statusIndex];
        
        // Рассчитываем follow-up только для активных процессов
        if (!['OFFER', 'REJECTED'].includes(status)) {
            const lastEmail = row[lastEmailIndex];
            // Проверяем, что дата существует и является строкой/датой
            const lastEmailDate = (lastEmail && typeof lastEmail === 'string') ? new Date(lastEmail) : (lastEmail instanceof Date ? lastEmail : new Date());
            
            // Ждем 5 дней для follow-up
            const followUpDelay = 5 * 24 * 60 * 60 * 1000; 
            const nextFollowUpDate = new Date(lastEmailDate.getTime() + followUpDelay);
            
            // Обновляем NextFollowup
            row[followUpIndex] = nextFollowUpDate.toISOString().slice(0, 10); 
            
            // Обновляем строку (диапазон начинается с 1, строка с i+1)
            sheet.getRange(i + 1, 1, 1, row.length).setValues([row]);
        }
    }
}

/**
 * Устанавливает CRON-триггер для запуска AutomationTask.
 * Запускать вручную один раз после настройки.
 */
function setupTrigger() {
    // Удаляем все предыдущие триггеры, чтобы избежать дублирования
    ScriptApp.getProjectTriggers().forEach(trigger => {
        if (trigger.getHandlerFunction() === 'AutomationTask') {
            ScriptApp.deleteTrigger(trigger);
        }
    });
    
    // Устанавливаем новый триггер: запускать каждые 15 минут
    ScriptApp.newTrigger('AutomationTask')
        .timeBased()
        .everyMinutes(15)
        .create();
    Logger.log('CRON trigger for AutomationTask set to run every 15 minutes.');
}
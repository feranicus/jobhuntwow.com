/**
 * JobHuntWOW: Google Apps Script Backend (SSO + API + Automation)
 *
 * This script now serves four purposes:
 * 1. doPost(e): Primary endpoint for SSO Login and data updates (POST requests).
 * 2. doGet(e): Provides multi-tenant data for the React frontend (GET requests).
 * 3. AutomationTask(): The scheduled CRON job to parse Gmail and update Google Sheets.
 * 4. Helper Functions: SSO validation and tenant management.
 */

// --- КОНСТАНТЫ И НАСТРОЙКИ ---
// !!! ОБЯЗАТЕЛЬНО ЗАМЕНИТЕ ЭТО НА ID ВАШЕЙ ТАБЛИЦЫ GOOGLE SHEETS !!!
const SPREADSHEET_ID = 'YOUR_SPREADSHEET_ID_HERE'; 
const SHEET_NAME_OPP = 'PipelineData'; // Лист для процессов
const SHEET_NAME_TENANTS = 'Tenants'; // Новый лист для пользователей

// Заголовки столбцов для листа PipelineData (обязательно добавьте tenantId в конец)
const HEADERS_OPP = [
    "ID", "Company", "Role", "Stage", "Status", "NextInterview", 
    "LastEmail", "HRContact", "NextFollowup", "ThreadURL", 
    "IsFavorite", "Notes", "SalaryExpectation", "Location", 
    "RecruiterCompany", "CVVersion", "JobURL", "ActivityFeed", "Insights", "tenantId" // <-- ДОБАВЛЕН
];

// Заголовки столбцов для листа Tenants
const HEADERS_TENANTS = [
    "tenantId", "email", "createdAt", "plan"
];

// --- Вспомогательные функции Sheets ---

function getSheet(name) {
    try {
        const ss = SpreadsheetApp.openById(SPREADSHEET_ID);
        const sheet = ss.getSheetByName(name);
        if (!sheet) throw new Error(`Sheet "${name}" not found.`);
        return sheet;
    } catch (e) {
        Logger.log('Error getting sheet: ' + e.message);
        return null;
    }
}

/**
 * Находит индекс строки по ID процесса.
 * @param {string} id Thread ID для поиска.
 */
function findRowIndex(id, sheetName) {
    const sheet = getSheet(sheetName);
    if (!sheet) return -1;
    // Ищем в первом столбце (A)
    const textFinder = sheet.getRange('A:A').createTextFinder(id);
    const result = textFinder.matchEntireCell(true).findNext();
    return result ? result.getRow() : -1;
}

// --- 1. API ENDPOINT ДЛЯ REACT (doPost & doGet) ---

/**
 * Обрабатывает POST-запросы (SSO Login, Update Status).
 */
function doPost(e) {
    try {
        const payload = JSON.parse(e.postData.contents);
        const { action, idToken, id, newStatus, tenantId } = payload;

        if (action === 'verifySso') {
            const userInfo = verifyIdToken_(idToken); 
            const tenant = getOrCreateTenant_(userInfo);
            
            // Возвращаем данные для React
            return createJsonResponse({
                tenantId: tenant.tenantId,
                email: userInfo.email,
                name: userInfo.name,
                picture: userInfo.picture,
            });
        } 
        
        else if (action === 'updateStatus') {
            // Проверка, что tenantId передан для обновления
            if (!tenantId) throw new Error("Authentication required for update.");
            return updateJobStatus_(id, newStatus, tenantId);
        }
        
        // ... (добавить другие POST-действия)
        
        throw new Error('Invalid action provided.');

    } catch (error) {
        Logger.log('doPost Error: ' + error.message);
        return createJsonResponse({ success: false, error: error.message }, 400);
    }
}

/**
 * Обрабатывает GET-запросы (получение данных для Канбана).
 * Обязательно требует tenantId для фильтрации.
 */
function doGet(e) {
    try {
        const tenantId = e.parameter.tenantId;
        
        // --- РЕЖИМ МУЛЬТИАРЕНДНОСТИ ---
        if (!tenantId || tenantId === 'DEMO') {
            // Если нет tenantId, мы в Demo Mode, возвращаем пустой массив
             return createJsonResponse([]);
        }
        
        // Получаем лист процессов
        const sheet = getSheet(SHEET_NAME_OPP);
        if (!sheet) throw new Error('Opportunities sheet unavailable.');
        
        // Получаем все данные
        const allValues = sheet.getDataRange().getValues();
        if (allValues.length <= 1) return createJsonResponse([]);
        
        const headerRow = allValues[0];
        const tenantIdIndex = headerRow.indexOf('tenantId');
        
        if (tenantIdIndex === -1) throw new Error("Sheet schema error: missing 'tenantId' column.");
        
        // Фильтрация по tenantId
        const filteredData = allValues.slice(1)
            .filter(row => row[tenantIdIndex] === tenantId);

        // Преобразование в JSON
        const jsonArray = filteredData.map(row => {
            const item = {};
            headerRow.forEach((header, index) => {
                let value = row[index];
                if (value instanceof Date) {
                    value = value.toISOString().slice(0, 10); // YYYY-MM-DD
                }
                item[header] = value;
            });
            return item;
        });

        return createJsonResponse(jsonArray);

    } catch (error) {
        Logger.log('doGet Error: ' + error.message);
        return createJsonResponse({ success: false, error: error.message }, 400);
    }
}

// --- 2. MULTI-TENANT & SSO HELPER FUNCTIONS ---

/**
 * Проверяет Google ID токен через официальный endpoint Google.
 * @param {string} idToken JWT token.
 * @returns {Object} User information.
 */
function verifyIdToken_(idToken) {
    const url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + idToken;
    
    // Внимание: UrlFetchApp требует, чтобы сервисы были разрешены в настройках Apps Script
    const resp = UrlFetchApp.fetch(url, { muteHttpExceptions: true }); 

    if (resp.getResponseCode() !== 200) {
        Logger.log("Token verification failed: " + resp.getContentText());
        throw new Error("Invalid ID token or verification error.");
    }
    const data = JSON.parse(resp.getContentText());

    // Можно добавить проверку 'aud' (должен совпадать с GOOGLE_CLIENT_ID) для повышения безопасности
    if (!data.email) {
        throw new Error("Token is valid, but email is missing.");
    }

    return {
        email: data.email,
        name: data.name || data.email,
        picture: data.picture,
        sub: data.sub, // Уникальный Google user id 
    };
}

/**
 * Ищет существующего арендатора или создает нового.
 * @param {Object} userInfo Результат verifyIdToken_.
 * @returns {Object} Объект с tenantId.
 */
function getOrCreateTenant_(userInfo) {
    // ВАЖНО: Убедитесь, что лист Tenants существует!
    const sheet = getSheet(SHEET_NAME_TENANTS);
    if (!sheet) throw new Error('Tenants sheet unavailable.');

    const data = sheet.getDataRange().getValues(); 
    
    const emailIndex = HEADERS_TENANTS.indexOf('email');
    const tenantIdIndex = HEADERS_TENANTS.indexOf('tenantId');

    // 1. Ищем существующего арендатора по email
    for (let i = 1; i < data.length; i++) {
        if (data[i][emailIndex] === userInfo.email) {
            Logger.log(`Tenant found for ${userInfo.email}: ${data[i][tenantIdIndex]}`);
            return { tenantId: data[i][tenantIdIndex] };
        }
    }
    
    // 2. Если не найден, создаем нового
    const newTenantId = "t_" + Utilities.getUuid();

    sheet.appendRow([newTenantId, userInfo.email, new Date(), "free"]);
    Logger.log(`New tenant created: ${newTenantId} for ${userInfo.email}`);

    return { tenantId: newTenantId };
}

/**
 * Обновляет статус процесса в Sheets после Drag & Drop.
 */
function updateJobStatus_(id, newStatus, tenantId) {
    const sheet = getSheet(SHEET_NAME_OPP);
    if (!sheet) throw new Error('Opportunities sheet unavailable.');
    
    const rowIndex = findRowIndex(id, SHEET_NAME_OPP);
    if (rowIndex === -1) throw new Error(`Job ID ${id} not found.`);
    
    const rowRange = sheet.getRange(rowIndex, 1, 1, HEADERS_OPP.length);
    const rowValues = rowRange.getValues()[0];
    
    const statusIndex = HEADERS_OPP.indexOf('Status');
    const stageIndex = HEADERS_OPP.indexOf('Stage');
    const tenantIdSheetIndex = HEADERS_OPP.indexOf('tenantId');

    // Проверка прав (ВАЖНО для мультиарендности)
    if (rowValues[tenantIdSheetIndex] !== tenantId) {
         throw new Error("Permission denied. Job does not belong to this tenant.");
    }

    // Обновляем Status (колонка Kanban)
    rowValues[statusIndex] = newStatus;
    // Обновляем Stage (прогресс-бар). Для D&D используем тот же статус.
    rowValues[stageIndex] = newStatus;
    
    // Записываем обновленную строку
    rowRange.setValues([rowValues]);

    return createJsonResponse({ success: true, message: `Status updated to ${newStatus}` });
}

// --- 3. GMAIL AUTOMATION TASK (CRON Trigger) ---

/**
 * Основная функция, которая запускается по CRON-триггеру (каждые 15 минут).
 */
function AutomationTask() {
    const sheet = getSheet(SHEET_NAME_OPP);
    if (!sheet) return;

    // В MVP мы не будем записывать новые процессы из Gmail, чтобы они не загрязняли 
    // приватные кабинеты других пользователей. AutomationTask будет только обновлять follow-up.
    
    generateFollowUpReminders(sheet);
}

/**
 * Пересчитывает NextFollowup для активных процессов.
 */
function generateFollowUpReminders(sheet) {
    const dataRange = sheet.getDataRange();
    const data = dataRange.getValues();
    
    if (data.length <= 1) return;

    const lastEmailIndex = HEADERS_OPP.indexOf('LastEmail');
    const statusIndex = HEADERS_OPP.indexOf('Status');
    const followUpIndex = HEADERS_OPP.indexOf('NextFollowup');

    for (let i = 1; i < data.length; i++) {
        const row = data[i];
        const status = row[statusIndex];
        
        if (!['OFFER', 'REJECTED'].includes(status)) {
            const lastEmail = row[lastEmailIndex];
            const lastEmailDate = (lastEmail && typeof lastEmail === 'string') ? new Date(lastEmail) : (lastEmail instanceof Date ? lastEmail : new Date());
            
            const followUpDelay = 5 * 24 * 60 * 60 * 1000; 
            const nextFollowUpDate = new Date(lastEmailDate.getTime() + followUpDelay);
            
            row[followUpIndex] = nextFollowUpDate.toISOString().slice(0, 10); 
            
            // Обновляем строку 
            sheet.getRange(i + 1, 1, 1, row.length).setValues([row]);
        }
    }
}


// --- 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

/**
 * Создает стандартизированный JSON-ответ с CORS-заголовками.
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

/**
 * Устанавливает CRON-триггер для запуска AutomationTask.
 */
function setupTrigger() {
    ScriptApp.getProjectTriggers().forEach(trigger => {
        if (trigger.getHandlerFunction() === 'AutomationTask') {
            ScriptApp.deleteTrigger(trigger);
        }
    });
    
    ScriptApp.newTrigger('AutomationTask')
        .timeBased()
        .everyMinutes(15)
        .create();
    Logger.log('CRON trigger for AutomationTask set to run every 15 minutes.');
}
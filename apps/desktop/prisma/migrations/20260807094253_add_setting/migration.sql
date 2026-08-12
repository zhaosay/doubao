-- CreateTable
CREATE TABLE "Setting" (
    "id" TEXT NOT NULL PRIMARY KEY DEFAULT 'singleton',
    "arkApiKey" TEXT,
    "indexTtsBaseUrl" TEXT DEFAULT 'http://10.39.64.13:7860',
    "updatedAt" DATETIME NOT NULL
);

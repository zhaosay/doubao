-- AlterTable
ALTER TABLE "Setting" ADD COLUMN "posterFontPath" TEXT;

-- CreateTable
CREATE TABLE "Poster" (
    "id" TEXT NOT NULL PRIMARY KEY,
    "projectId" TEXT NOT NULL,
    "presetId" TEXT NOT NULL,
    "title" TEXT NOT NULL,
    "subtitle" TEXT,
    "extraPrompt" TEXT,
    "referenceImagePaths" TEXT,
    "backgroundPath" TEXT,
    "filePath" TEXT,
    "status" TEXT NOT NULL DEFAULT 'pending',
    "error" TEXT,
    "providerId" TEXT,
    "model" TEXT,
    "createdAt" DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT "Poster_projectId_fkey" FOREIGN KEY ("projectId") REFERENCES "Project" ("id") ON DELETE CASCADE ON UPDATE CASCADE
);

-- CreateIndex
CREATE INDEX "Poster_projectId_idx" ON "Poster"("projectId");

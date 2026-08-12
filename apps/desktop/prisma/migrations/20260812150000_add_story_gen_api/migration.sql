-- AlterTable
ALTER TABLE "Setting" ADD COLUMN "storyGenProvider" TEXT NOT NULL DEFAULT 'claude_cli';
ALTER TABLE "Setting" ADD COLUMN "storyGenApiBaseUrl" TEXT;
ALTER TABLE "Setting" ADD COLUMN "storyGenApiKey" TEXT;
ALTER TABLE "Setting" ADD COLUMN "storyGenApiModel" TEXT;
ALTER TABLE "Setting" ADD COLUMN "storyGenApiMaxTokens" INTEGER NOT NULL DEFAULT 4096;

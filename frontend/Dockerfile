FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY tsconfig.json vite.config.ts index.html ./
COPY src ./src

RUN npm run build

FROM node:20-alpine AS runtime

WORKDIR /app
ENV NODE_ENV=production

COPY --from=build /app/dist ./dist

EXPOSE 5173

CMD ["npx", "--yes", "serve", "-s", "dist", "-l", "5173"]

FROM nginx:1.27-alpine

# Remove default nginx config and page
RUN rm /etc/nginx/conf.d/default.conf \
    && rm -rf /usr/share/nginx/html/*

# Copy custom nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy website
COPY index.html /usr/share/nginx/html/index.html
COPY kevin.png /usr/share/nginx/html/kevin.png

# Non-root user (nginx-alpine supports this)
RUN chown -R nginx:nginx /usr/share/nginx/html \
    && chmod -R 755 /usr/share/nginx/html

ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -qO- http://localhost/health || exit 1

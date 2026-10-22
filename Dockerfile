FROM golang:1.26-alpine AS build

# The official golang image sets GOTOOLCHAIN=local. The nightly security job can raise
# the go directive past the image's Go patch release; let the build fetch that toolchain
# instead of failing until the image catches up.
ENV GOTOOLCHAIN=auto

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN find static -type f \( -name '*.js' -o -name '*.css' \) -exec gzip -k -9 {} \;
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /ollamail .

FROM alpine:3.24

RUN apk add --no-cache tzdata && adduser -D -u 1000 -s /sbin/nologin appuser

COPY --from=build /ollamail /ollamail

USER appuser

EXPOSE 5000

CMD ["/ollamail"]

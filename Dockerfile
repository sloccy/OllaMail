FROM --platform=$BUILDPLATFORM golang:1.24-alpine AS build

ARG TARGETOS
ARG TARGETARCH

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH go build -ldflags="-s -w" -o /ollamail .

FROM alpine:3.21

RUN adduser -D -u 1000 -s /sbin/nologin appuser

COPY --from=build /ollamail /ollamail
COPY templates/ /templates/
COPY static/ /static/

USER appuser

EXPOSE 5000

CMD ["/ollamail"]

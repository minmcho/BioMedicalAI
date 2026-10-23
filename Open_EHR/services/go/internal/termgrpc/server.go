package termgrpc

import (
	"context"
	"encoding/json"

	v1 "opencare/services/gen/opencare/v1"

	"google.golang.org/grpc"
)

// Server implements opencare.terminology.v1.TerminologyService.
type Server struct {
	v1.UnimplementedTerminologyServiceServer
}

// Normalise carries the batch contract: JSON rows in, JSON rows out.
func (s *Server) Normalise(_ context.Context, req *v1.NormaliseRequest) (*v1.NormaliseResponse, error) {
	mappings := make([]Entry, 0, len(req.GetMappings()))
	for _, m := range req.GetMappings() {
		mappings = append(mappings, Entry{
			FromCode:   m.GetFromCode(),
			FromSystem: m.GetFromSystem(),
			ToCode:     m.GetToCode(),
			ToSystem:   m.GetToSystem(),
		})
	}
	rows := make([]json.RawMessage, 0, len(req.GetRowsJson()))
	for _, r := range req.GetRowsJson() {
		rows = append(rows, json.RawMessage(r))
	}
	outcome, err := Normalise(mappings, rows)
	if err != nil {
		return nil, err
	}
	resp := &v1.NormaliseResponse{
		UnmappedCodes: outcome.UnmappedCodes,
		UnmappedRows:  int32(outcome.UnmappedRows),
		MappedRows:    int32(outcome.MappedRows),
	}
	for _, r := range outcome.Rows {
		resp.RowsJson = append(resp.RowsJson, string(r))
	}
	return resp, nil
}

// Register attaches the service to a gRPC server.
func Register(gs *grpc.Server) {
	v1.RegisterTerminologyServiceServer(gs, &Server{})
}

// NewClient dials a TerminologyService (loopback in tests; a site
// address in deployment) and returns the generated client.
func NewClient(conn *grpc.ClientConn) v1.TerminologyServiceClient {
	return v1.NewTerminologyServiceClient(conn)
}

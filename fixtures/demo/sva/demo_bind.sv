bind demo_lane stream_hold_sva #(.DATA_W(DATA_W)) u_stream_hold_sva (
  .clk   (clk),
  .rst_n (rst_n),
  .valid (valid_o),
  .ready (ready_i),
  .data  (data_o)
);

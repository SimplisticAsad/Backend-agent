# ac.ticket.browse  (requirements: requirement.ticket_management; operations: operation.ticket.list, operation.ticket.read)
Feature: Browse Tickets succeeds

  Scenario: Browse Tickets succeeds
    Given the user is signed in as Customer or Agent or Manager
    And the record being worked on exists
    When List Tickets
    And View Ticket
    Then The Ticket information is displayed
